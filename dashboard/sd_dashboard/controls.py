"""Small forms over the workflow library; no status rules live in the UI."""

from __future__ import annotations

import json

from sd_db import repos, workflow

from .markup import Markup, join, tag


def field(label, name, value="", *, type="text", required=False, **attributes):
    identifier = attributes.pop("id", f"field-{name}")
    return tag("div", tag("label", label, for_=identifier),
               tag("input", name=name, id=identifier, value=value or "",
               type=type, required=required, **attributes), class_="field")


def select(label, name, choices, selected=None, *, id=None):
    identifier = id or f"field-{name}"
    return tag("div", tag("label", label, for_=identifier), tag("select", join(
        tag("option", label, value=value, selected=str(value) == str(selected))
        for value, label in choices), name=name, id=identifier), class_="field")


def textarea(label, name, value="", *, id=None, rows=4, required=False):
    identifier = id or f"field-{name}"
    return tag("div", tag("label", label, for_=identifier),
        tag("textarea", value, id=identifier, name=name, rows=rows,
            required=required, maxlength=50000), class_="field")


def form(action, *fields, label, command, revision=None, compact=False, changed_fields=False):
    return tag("form",
        tag("input", type="hidden", name="revision", value=revision) if revision else Markup(""),
        join(fields),
        tag("div", tag("button", label, type="submit", class_="primary"), class_="form-actions"),
        tag("p", "", class_="form-result", role="status", aria_live="polite"),
        method="post", action=action, data_workflow_form=True,
        data_cli=command,
        data_changed_fields=changed_fields,
        class_="workflow-form compact-form" if compact else "workflow-form")


def _checkouts(connection):
    """The repositories a task may belong to, as `select()` choices.

    `repos.registered` -- every row the `repo` table holds. That is exactly
    the domain `workflow._fields` validates `repo` against: a bare existence
    check on `repo.path`, with no condition on the remote. The runner screen
    offers the narrower remote-only set because a run has to clone from one;
    belonging to a checkout does not, so a task may sit in a checkout that has
    no remote and this field has to be able to say so.

    The blank choice is the clear. It is first, so a task with no repository
    gets it as the browser's default selection and `data-changed-fields` reads
    the field as unchanged -- an omitted key, which is not a null.

    There is deliberately no counterpart to the `Existing priority` option
    above. That one exists because `item.priority` carries no constraint, so
    the column holds values the form never offered. `item.repo` is a foreign
    key onto `repo.path` whose removal rule is `RESTRICT`, so a stored path is
    always a registered path: this list already holds every value the column
    can carry, and the function needs no "current value" argument.
    """
    choices = [("", "No repository")]
    choices.extend((row["path"], row["path"]) for row in repos.registered(connection))
    return choices


def _status_control(connection, item_id, row, revision, statuses=None):
    if statuses is None:
        statuses = workflow.allowed_statuses(connection, item_id)
    return (form(f"/api/items/{item_id}/status",
        select("Status", "status", [(value, value.replace("_", " ")) for value in statuses], row["status"]),
        field("Reason (optional)", "reason"), label="Change status",
        command=f"sd task status {item_id} STATUS", revision=revision, compact=True)
        if statuses else tag("p", "Status is controlled by the active workflow.", class_="hint"))


def _unpark_control(item_id, row, revision):
    # sd:3007: the nightly prune parks an untouched P4 item; this brings it back. A writing piece revives
    # through its own control in `writing_controls`, which moves its file.
    if not row["parked_at"] or row["piece"] is not None:
        return Markup("")
    return tag("section", tag("h2", "Parked"),
        tag("p", f"Parked {row['parked_at'][:10]}: this item is off Today and the task lists. "
                 "Unpark brings it back with its fields and history.", class_="hint"),
        form(f"/api/items/{item_id}", tag("input", type="hidden", name="parked_at", value=""),
             label="Unpark", command=f"sd task edit {item_id} --unpark", revision=revision, compact=True),
        class_="control-panel")


def item_controls(connection, item_id):
    state = workflow.item_state(connection, item_id)
    row, revision = state["item"], state["revision"]
    return join((_unpark_control(item_id, row, revision), _item_controls(connection, item_id, row, revision)))


def _item_controls(connection, item_id, row, revision):
    if workflow.hand_idea(row):
        # pack sd:9302: the status control alone. `edit_item` takes no idea's
        # details (`DETAIL_KINDS`), so a details form would offer a refused save.
        # pack sd:9380: an idea reclassified from a task keeps its status listed
        # and selected, so one press does not silently write planning.
        statuses = workflow.allowed_statuses(connection, item_id)
        if statuses and row["status"] not in statuses:
            statuses = [row["status"], *statuses]
        return tag("section", tag("h2", "Manage this item"),
            tag("div", _status_control(connection, item_id, row, revision, statuses),
                tag("p", "An idea can close here, or reopen to planning.", class_="hint")),
            class_="control-panel")
    if row["kind"] not in ("task", "work", *workflow.TASK_STATUS_KINDS):
        return Markup("")
    if row["kind"] == "work":
        from sd_db import progress

        if progress.work_controls(connection, item_id)["reason"]:
            return Markup("")
    priorities = [("", "Unprioritized"), (1, "1 · Highest"), (2, "2 · High"),
                  (3, "3 · Normal"), (4, "4 · Low")]
    if row["priority"] is not None and row["priority"] not in (1, 2, 3, 4):
        priorities.append((row["priority"], f"{row['priority']} · Existing priority"))
    inputs = [field("Title", "title", row["title"], required=True, maxlength=500),
              tag("div", select("Priority", "priority", priorities, row["priority"]),
                  field("Due", "due", row["due"], type="date"), class_="field-row")]
    if row["kind"] in ("task", "followup"):
        # Not for work, exactly like `body` below and for the same reason: a
        # work item's repository is part of its source identity, and
        # `workflow.edit_item` refuses to reassign it. Rendering the field for
        # a work item would offer a choice whose every non-default value the
        # library rejects -- and rejects the whole save with it, title included.
        inputs.append(select("Repository", "repo", _checkouts(connection), row["repo"]))
    if row["kind"] == "task":
        # Task only. A work row's kind is its source identity and the library
        # refuses to change it. A followup's is not, but reclassifying one is
        # not among the details sd:809 opened and sd:816 asked for, so the
        # select stays off its form; the choices here are the kinds a hand edit
        # may set, so it offers nothing `edit_item` will refuse by kind.
        # Its own id: `note_capture` on the same page owns `field-kind`.
        #
        # The hand kinds the store's own CHECK admits, not the constant whole.
        # This page reads through `connect(write=False)`, which accepts a
        # store older than migration 009, and on one `edit_item` refuses each
        # of the four kinds that migration added -- by name, from the same
        # `schema_kinds` read -- so offering them was offering a save the
        # library rejects (PR #332 review). A CHECK the library cannot read is
        # a store where every kind change is refused, so the select then
        # holds the current kind alone.
        try:
            admitted = workflow.schema_kinds(connection)
        except workflow.WorkflowError:
            admitted = (row["kind"],)
        inputs.append(select("Kind", "kind", [(kind, kind) for kind in workflow.HAND_KINDS if kind in admitted],
                             row["kind"], id="item-kind"))
    if row["kind"] in ("task", "followup"):
        try:
            body = json.loads(row["body"] or "{}")
        except ValueError:
            # sd:870, the twin of sd:775's hole on the report panel. `item.body`
            # is plain `TEXT` with no `json_valid` check, so a row this cannot
            # parse is a row the store permits; unguarded, `json.JSONDecodeError`
            # raised through `screens.item` and past `do_GET`, which catches
            # `MissingItem`, `NotFound` and the database errors and not a
            # `ValueError`, and the reader got no response at all.
            #
            # An empty textarea would be worse here than no field: the save
            # beside it would write that blank over the stored body nothing
            # could read. Withhold the field -- `data-changed-fields` posts only
            # what the reader changed, and a field that is not rendered is never
            # sent -- and say why, so the other details still save.
            inputs.append(tag("p", "This item's stored body is not valid JSON, so the details "
                                   "field is withheld and no save here can overwrite it. Repair "
                                   "the stored text with ",
                              tag("code", f"sd task edit {item_id}"), ".", class_="notice"))
        else:
            value = body.get("text", "") if isinstance(body, dict) else ""
            inputs.append(textarea("Details", "body", value))
    return tag("section", tag("h2", "Manage this item"),
        tag("div", form(f"/api/items/{item_id}", *inputs, label="Save details",
                        command=f"sd task edit {item_id} --title TITLE", revision=revision, changed_fields=True),
            tag("div", _status_control(connection, item_id, row, revision),
                tag("p", "A followup can finish or reopen here." if row["kind"] == "followup" else
                    "A task can finish here. Code delivery keeps its delivery checks.", class_="hint")),
            class_="item-controls"), class_="control-panel")


def note_capture(connection, item_id):
    revision = workflow.item_state(connection, item_id)["revision"]
    return form(f"/api/items/{item_id}/notes",
        textarea("Add a note", "body", id="note-body", rows=3, required=True),
        select("Kind", "kind", [(value, value.capitalize()) for value in
            ("comment", "followup", "question", "decision", "proposal")], "comment"),
        label="Add note", command=f"sd task note {item_id} TEXT --kind comment", revision=revision)


def note_resolution(note, revision):
    if note["resolved_at"]:
        return tag("span", "Resolved", class_="status status-done")
    if note["kind"] not in ("comment", "followup", "question", "decision", "proposal"):
        return tag("span", "Recorded", class_="hint")
    return form(f"/api/notes/{note['id']}/resolve", label="Resolve",
                command=f"sd note resolve {note['id']}", revision=revision, compact=True)


def progress_controls(connection, item_id):
    from sd_db import progress

    state = workflow.item_state(connection, item_id)
    available = progress.work_controls(connection, item_id)
    row, revision = state["item"], state["revision"]
    if row["kind"] != "work":
        return Markup("")
    controls = []
    if available["relink"]:
        controls.append(form(f"/api/items/{item_id}/relink",
            field("Artifact path within the repository", "path", row["path"], required=True),
            label="Update artifact link", command=f"sd work relink {item_id} PATH",
            revision=revision))
    if available["cancel"]:
        controls.append(tag("details", tag("summary", "Cancel this work item"),
            tag("p", "Records why this work stopped. It does not mark code as delivered or remove files.", class_="hint"),
            form(f"/api/items/{item_id}/cancel",
                field("Reason for cancelling", "reason", id="cancel-reason", required=True),
                label="Cancel work", command=f"sd work cancel {item_id} --reason TEXT", revision=revision)))
    if not controls:
        return tag("p", available["reason"], class_="hint") if available["reason"] else Markup("")
    return tag("section", tag("h2", "Work references"), join(controls), class_="control-panel")


def writing_controls(connection, row, *, state=None):
    if row["kind"] != "idea" or row["piece"] is None:
        return Markup("")
    from sd_db import writing

    state = state or writing.piece_state(connection, row["id"])
    detail, revision = state["writing"], state["revision"]
    item_id = row["id"]
    gates = detail["gates"]
    body = [tag("h2", "Writing progress"),
            tag("p", tag("strong", detail["stage"].capitalize()), " · ", row["piece"]),
            tag("h3", "Review checks pass" if gates["ok"] else "Review needs attention"),
            tag("ul", join(tag("li", problem) for problem in gates["problems"]))
            if gates["problems"] else tag("p", "Checks match the current draft.", class_="hint")]
    for publication in detail.get("publications", []):
        phase = publication["phase"] if isinstance(publication["phase"], str) else ""  # a claim's JSON may hold any value
        status = "Publication needs reconciliation" if publication.get("reconcile_required") else ("Publication " + phase).strip()
        body.append(tag("article", tag("h3", status),
            tag("p", "Claim " + publication["id"], class_="hint"),
            tag("p", "This receipt covers an earlier draft. Review the current draft before publishing again.", class_="hint")
            if publication.get("current_draft_published") is False else "",
            tag("p", tag("a", "Open published document", href=publication["url"], rel="noopener noreferrer")) if publication.get("url") else "",
            tag("p", "Resume from the saved claim; check the destination before repeating an interrupted operation.", class_="hint") if phase != "published" else "",
            tag("code", f"sd writing publication-status --piece {row['piece']} --claim {publication['id']}"), class_="operation-card"))
    if not detail["owner"]:
        body.append(tag("p", "This piece has no registered repository, so it cannot move.", class_="hint"))
    elif detail["owner"] != "row":
        body.append(tag("p", "This collection is read-only until its database migration is verified.", class_="hint"))
    else:
        parked = detail["parked"]
        action = "revive" if parked else "park"
        body.append(tag("details", tag("summary", "Revive this piece" if parked else "Park this piece"),
            tag("p", "Bring this piece back to the writing list." if parked else
                "Set this piece aside while preserving its stage and review history.", class_="hint"),
            form(f"/api/items/{item_id}/{action}", label="Revive piece" if parked else "Park piece",
                command=f"sd writing park --piece {row['piece']}" + (" --revive" if parked else ""), revision=revision)))
        stages = detail["available_stages"]
        if stages:
            body.append(form(f"/api/items/{item_id}/stage",
                select("Next stage", "stage", [(value, value.capitalize()) for value in stages]),
                field("Reason (optional)", "reason", id="writing-reason"),
                label="Move to stage", command=f"sd writing stage --piece {row['piece']} --stage STAGE", revision=revision))
        corrections = detail["correction_stages"]
        if corrections:
            body.append(tag("details", tag("summary", "Correct an earlier stage"),
                tag("p", "Record why this piece needs to return to an earlier stage. Its review checks will need to be renewed.", class_="hint"),
                form(f"/api/items/{item_id}/stage",
                    tag("input", name="correct", value="true", type="hidden"),
                    select("Return to stage", "stage", [(value, value.capitalize()) for value in corrections], id="correction-stage"),
                    field("Reason for correction", "reason", id="correction-reason", required=True),
                    label="Correct stage", command=f"sd writing stage --piece {row['piece']} --stage STAGE --correct --reason TEXT", revision=revision)))
    return tag("section", join(body), class_="control-panel writing-controls")
