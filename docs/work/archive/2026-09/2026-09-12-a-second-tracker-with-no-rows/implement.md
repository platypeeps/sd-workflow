# Implement — a second tracker with no rows

## Budget and shape

The port crosses the boundary sd:392 names: the collector and the dispatch
land in `platypeeps/system` (`local-sd-db`), the verb and the reader land
here, and the pin between them moves in its own pull request because
`.github/sd-review.json` lists `.github/workflows/**` as sensitive. That is
the sequence #888 followed for sd:360 and it is followed again here, step
for step. Nothing in the pack half depends on the newer library until the
pin moves: the verb reads `TRACKERS` and `configured` through `getattr` with
today's behaviour as the default, and that is asserted by running the verb's
tests with the pinned build, not assumed.

`dashboard/` changes by one expression, step 7b, so the page shows a Jira
row's key instead of its project. `DASHBOARD_CAP`
(in `tests/test_loc_caps.py`, retired at sd:719 step 7) and `DASHBOARD_CODE_CAP`
(in `tests/test_loc_caps.py`, retired at sd:719 step 7) do not move; `dashboard/jira.py` stayed until
the `index.sqlite` retirement deleted it (sd:719 step 4). The pack's `bin/` has no ceiling
since R11-D48. The pack half is on the order of forty lines in
`bin/sd_shadow.py`, thirty in `bin/sd-status`, and their tests.

## Step checklist

- [x] **1. Configure, before any code.** Export `JIRA_BASE_URL` and
      `JIRA_EMAIL` in `~/.config/shell/env.sh`, beside the `JIRA_API_TOKEN`
      that is already there, because that file is what
      `local-cron-jobs/cron-jobs.sh` sources before the nightly job and what
      a login shell reads. This is the operator's step; the PRD's "if yes"
      branch puts it first and this checklist does not reorder it.
      Verify: in a fresh login shell, a presence check that prints the
      *missing* names and never a value —
      `python3 -c 'import os; print([n for n in ("JIRA_BASE_URL","JIRA_EMAIL","JIRA_API_TOKEN") if not os.environ.get(n, "").strip()])'`
      — prints `[]`. The
      `.strip()` matches `settings` (in `dashboard/jira.py`, retired at sd:719 step 4), which strips
      before `missing` (in `dashboard/jira.py`, retired at sd:719 step 4) looks; a value of spaces
      would otherwise pass this check and fail step 2.
      Done 2026-09-13: the operator exported both names beside the token, and
      the check prints `[]` where before this step it printed
      `['JIRA_BASE_URL', 'JIRA_EMAIL']`.

- [x] **2. One proving collect against the loop that exists.** Run
      `bin/sd-dashboard index` from the checkout — the verb at
      `bin/sd-dashboard` (line 32 as of sd:361, retired at sd:719 step 4), declared at `bin/sd-dashboard` (lines 97-103 as of sd:361, retired at sd:719 step 4).
      Use the calling convention (`AGENTS.md:49-61`) with the three variables exported. This is the PRD's "one successful collect
      against the existing `dashboard/collect.py` (line 168 as of sd:361, retired at sd:719 step 4) loop proving a row can
      be produced at all", and it costs no code.
      Verify, in four checks. None of them reads a missing row as a verdict:
      an absent row is ambiguous, because the JQL window excludes issues the
      credentials reach perfectly well, so a query for one named key cannot
      tell a wrong account from an old ticket.
      (a) The run prints `issues[jira]: N new, 0 updated, M open` with
      `N > 0`, rather than `issues[jira]: not collected (...)`. It printed
      `issues[jira]: 12 new, 0 updated, 11 open` on 2026-09-13.
      (b) `sqlite3 "$(python3 -c 'from dashboard import store; print(store.index_path())')" "select tracker, count(*) from issue group by tracker"`
      — the path derived, because `index_path` honours `XDG_CACHE_HOME` —
      returns a `jira` row where before this step it returned `github` alone.
      It returned `github|1824` and `jira|12`.
      (c) `python3 -c 'from dashboard import jira; print(jira.account_id(jira.settings())[0])'`
      equals the operator's own accountId, read from Jira by hand. This is
      the direct question, and it is the check that carries the design's last
      risk: a token minted for another account collects that account's
      involvement and reports success. It answered the operator's
      own account id (redacted, sd:8824), the account that both files
      and is assigned `LOG-23818`, so the token is the operator's own.
      (d) A specific issue key is named only after asserting that its
      `updated` date falls inside `FIRST_RUN_WINDOW` (in `dashboard/jira.py`, retired at sd:719 step 4);
      otherwise take the newest key the query itself returned. `LOG-23818`,
      the key the item seeds, does not qualify: it was last updated
      2026-05-05, 131 days before the run, and `DEFAULT_JQL`
      (in `dashboard/jira.py`, retired at sd:719 step 4) filters `updated >= -{minutes}m`, so the
      collector is never asked for it. The run returned `LOG-21895`,
      `LOG-23702`, `LOG-23929`, `RS-8`, `RS-9`, `RS-45`, `RS-47`, `RS-48`,
      `RS-49`, `RS-50` and `RS-51` open, and `RS-54` closed — eleven open and
      one closed, which is the docstring's window in place of an open-only
      filter. `LOG-23929` is the newest of them and is the key later steps
      name.
      If the run prints `Jira rejected the credentials`, stop: the port does
      not start on credentials that do not reach Jira.

- [x] **3. The library module: `sd_db/shadow_jira.py`.** In
      `platypeeps/system`, lift `dashboard/jira.py` (the pack module as it stood at `2a2dbad6`; retired at sd:719 step 4) whole — module docstring
      with both lists, `settings`, `missing`, `window_start`,
      `window_minutes`, `_request`, `account_id`, `search`, `state_of`,
      `normalize`, `collect` — with four changes and no others. (i) `collect`
      returns `Collected` rather than the pack's dict. (ii) Every use of the
      pack's `github` module goes: `window_start` calls the library's
      `parse_iso` at `sd_db/shadow_sync.py:119`, and the three `github.iso`
      calls in `collect` (in `dashboard/jira.py`, retired at sd:719 step 4, on its missing-variable,
      failed-`myself` and normal returns) call the library's `iso` at
      `sd_db/shadow_sync.py:114`; `from . import github` appears nowhere in
      the new module, and an import of it is the first thing the ported test
      module would fail on. (iii) `ok` folds truncation in: the pack's
      `collect` sets `ok` from `not error` alone at line 322 of `dashboard/jira.py` at `2a2dbad6`
      and reports `truncated` beside it, because `refresh_issues` reads only
      `ok` and the pack never advanced a cursor over a truncated page only
      because `truncated` was reported, not because it was guarded. The
      library's `Collected` is `ok=not errors and not truncated`
      (`sd_db/shadow_sync.py:466`), and the port returns
      `ok=not error and not cut`, so the watermark guard on `ok` in step 4
      is sound. (iv) `fetch_issue` (in `dashboard/jira.py`) did not move,
      because it belonged to `sd-trackers ref`, which this item left as it
      was; both retired at sd:719 step 4 (pack pull request #1005), and the
      reference path is the shadow-row procedure in `skills/sd-plan/SKILL.md`.
      `TRACKER =
      "jira"`, `OVERLAP` and `FIRST_RUN_WINDOW` are declared in the module,
      not shared, for the reason `window_start` (in `dashboard/jira.py`, retired at sd:719 step 4)
      gives. Port `JiraTests` (in `tests/test_sd_dashboard_index.py`, retired at sd:719 step 4) with
      its `jira_issue` and `jira_transport` fixtures to
      `local-sd-db/tests/test_shadow_jira.py`, asserting on `Collected`
      fields. The suite is not free of the pack:
      `test_the_window_is_relative_minutes_not_a_timestamp` calls
      `github.iso(...)` at line 493 of `tests/test_sd_dashboard_index.py` at `2a2dbad6` through
      the module's `dashboard.github` import, so the port replaces that call
      with the library's `iso` at `sd_db/shadow_sync.py:114` and drops the
      import; a copy that keeps it fails on collection in `local-sd-db`,
      where there is no `dashboard` package.
      Verify: the ported suite passes, and each of the three carried-over
      rules is guarded by a test that goes red under the mutation that
      breaks it. The rules, as lines 254-258 of `dashboard/jira.py` at `2a2dbad6` implements them:
      `myself` is the availability check; account ids are compared when both
      sides have one and email only as the fallback; the JQL window is
      relative minutes. The mutations, each of which must redden its test
      and be reverted with `diff -q` reporting the tree identical: replace
      the `myself` call with the search itself (the empty-200 case passes
      as "no issues"); compare by email even when both ids are present;
      format the JQL with an absolute timestamp. A fourth, for (iii): set
      `ok` from `not error` alone and the truncation test in step 4 (c)
      reddens. Control: a transport returning one `To Do` issue yields
      exactly one `Collected.issues` row with `tracker == "jira"`,
      `number is None`, `state == "open"`.
      Done 2026-09-13: system #312 (squash b05d684a) added the module and its
      ported suite, with all four mutations reddening their tests; system
      #320 (squash 05b03680) then rewrote the stale "retires with no
      successor" docstring to name the new module.

- [x] **4. The library dispatch, the export, and the docstring.** In
      `sd_db/shadow_sync.py`: `sync` keeps its signature; `tracker="github"`
      runs the path at `sd_db/shadow_sync.py:595-661` unchanged;
      `tracker="jira"` runs a new `_sync_jira` — `read_watermark`, `collect`,
      then under `transaction` `store`, re-read the cursor, `write_watermark`
      only when `result.ok` and the cursor has not moved past `moment`, then
      the `tracker-sync:jira` heartbeat with the same body keys GitHub's
      writes at `sd_db/shadow_sync.py:638-646`, with `queued` 0 and
      `incomplete` empty; any other `tracker` raises `ValueError` naming
      `TRACKERS`. `Synced` gains `configured: bool = True`, last, after
      `incomplete`, so positional construction anywhere is unaffected;
      `_sync_jira` sets it `False` when `missing` is non-empty, writes the
      heartbeat with `ok False` and the reason, and returns before any
      request. The budget arguments `max_requests` and `max_seconds` are
      validated and otherwise unused on this path — Jira's ceiling is
      `MAX_PAGES` — and the docstring says so rather than accepting them
      silently. `sd_db/__init__.py` exports `TRACKERS =
      ("github", "jira")`. The paragraph at `sd_db/shadow_sync.py:25-29`
      is rewritten to name the second module, the caller's iteration, and
      the 2026-09-12 decision on sd:361.
      Verify, each as a test in `local-sd-db/tests/test_shadow_sync.py` or
      beside it: (a) `sync(connection, tracker="jira", ...)` with a
      transport fixture, on a database seeded with
      `write_watermark(connection, "github", <known stamp>)`, writes `jira`
      rows, writes a `watermark` row with `key = 'jira'`, and leaves
      `read_watermark(connection, "github")` equal to the seeded stamp —
      exactly, not merely non-`None`, because an empty database would let a
      cross-tracker overwrite pass; and the mirror, a GitHub sync on a
      database seeded with a `jira` watermark leaves the `jira` stamp
      exactly as seeded — the per-tracker rule, as a fact about the `state`
      table; (b)
      the same call with `JIRA_BASE_URL` absent from the environment writes
      no row, writes no watermark, makes no request, writes one
      `tracker-sync:jira` heartbeat whose body carries the reason, and
      returns `ok False, configured False`; (c) a transport that returns `isLast: false` with
      no token returns `ok False, truncated == ["jql"]`, the rows it did
      return are stored, and the watermark is not written — two mutations,
      each of which reddens (c): drop the `result.ok` guard in `_sync_jira`,
      and set `ok` from `not error` alone in `shadow_jira.collect`; (d)
      `sync(connection, tracker="nope")` raises `ValueError`; (e) the
      GitHub path's existing tests are unchanged and green, which is the
      control that the dispatch did not touch them. Then
      `grep -n "retires with no successor" sd_db/shadow_sync.py` prints
      nothing — the item's last acceptance line, as a grep. Suites under
      the CI-shaped venv as sd:603 recorded them: `local-sd-db`, dashboard,
      runner, all OK.
      Done 2026-09-13: system #338 (squash ee68c3e8) landed the dispatch,
      `configured`, and the `TRACKERS` export, with tests (a) to (d) and the
      mutations its body records; the library suite ran 996 tests OK.

- [x] **5. The pin.** `.github/workflows/tests.yml:90` moves from `758dfb48`
      to the squash SHA of step 4's merge. Its own pull request, because the
      file is sensitive; nothing else in it.
      Verify: CI on that pull request is green with the new pin, and
      `python -c "import sd_db; print(sd_db.TRACKERS)"` in the CI venv prints
      `('github', 'jira')`.
      *Done by pack #949 (`f9123117`), which moved the pin in
      `.github/workflows/tests.yml` to `09260ad4`, past step 4's merge; that
      commit exports `sd_db.TRACKERS == ('github', 'jira')`, and so does
      every pin after it.*

- [x] **6. The verb iterates.** `shadow_sync` (`source:bin/sd_shadow.py::shadow_sync`) reads
      `names = getattr(sd_db, "TRACKERS", ("github",))`; when `--since` or
      `--until` is given, `names` is `("github",)` and every other tracker
      prints `shadow sync[<name>]: skipped (recovery window is GitHub's)`.
      For each name it calls `sd_db.sync_shadow(connection, tracker=name,
      **options)` and `report_sync` (`source:bin/sd_shadow.py::report_sync`), which gains the
      name and prefixes every line it prints with `shadow sync[<name>]:`.
      The lines `_library_lines` (`source:bin/sd_shadow.py::_library_lines`) forwards already
      begin `shadow sync: ` — `Synced.report` at `sd_db/shadow_sync.py:547`
      writes that head on each, and the test double at
      `tests/test_sd_suggest.py:742-743` reproduces it — so the verb strips
      that head before adding its own, and never prints
      `shadow sync[jira]: shadow sync: ...`. A forwarded line that does not
      carry the head is prefixed as it is.
      When `getattr(result, "configured", True)` is false, `report_sync`
      prints the single line `shadow sync[<name>]: not collected (<reason>)`
      and returns 0 regardless of `--strict`. The exit code is the maximum
      over trackers. `docs/workflow-controls.md:197-205` is rewritten, not
      appended to: its first sentence, "`sd shadow sync --strict` fails when
      any requested tracker interval remains incomplete", becomes "fails
      when any *configured* tracker's interval remains incomplete; a tracker
      whose variables are unset is reported as not collected and does not
      fail the run", and a sentence follows saying the recovery flags bound
      GitHub only. Left as it stands, the paragraph would tell the operator
      that an unconfigured Jira fails the nightly.
      Verify: in `tests/test_sd_suggest.py`, beside `TheShadowSync`
      (`source:tests/test_sd_suggest.py::TheShadowSync`): (a) with the pinned library, the
      four existing tests pass unchanged — the substring assertions such as
      `wrote 2 shadow row(s)` still hold under the prefix, which is the
      control that the pin can lag; (b) with a fake `sd_db` whose `TRACKERS`
      is `("github", "jira")` and whose `sync_shadow` returns a `Synced`
      with `configured=False` for `jira`, `--strict` exits 0 and the output
      carries `shadow sync[jira]: not collected (JIRA_BASE_URL and
      JIRA_EMAIL not set)` — the item's second acceptance line — and, with
      the fake's `report()` returning a `shadow sync: contribution detail
      backlog: 3 queued` line for `github`, the output carries exactly
      `shadow sync[github]: contribution detail backlog: 3 queued` and no
      line containing `shadow sync[github]: shadow sync:`; (c) same
      fake, `jira` returning `ok=False, configured=True, reason="Jira
      rejected the credentials (401 Unauthorized); check JIRA_EMAIL and
      JIRA_API_TOKEN"`, `--strict` exits 1 and the reason is printed — the
      fourth line; (d) `--since` given, `sync_shadow` is called once, with
      `tracker="github"`, and the skipped line is printed for `jira`.
      Mutation: make the unconfigured branch fall through to `cursor held`
      and (b) reddens on the exit code.
      *Done 2026-09-15 on pack branch
      `fix/sd-361-step-6-the-verb-iterates-trackers`: `TheShadowSyncOverTrackers`
      in `tests/test_sd_suggest.py` carries (b) to (d) against a fake `sd_db`,
      (a) is `TheShadowSync` on the pinned library, and the four mutations the
      pull request body records each reddened.*

- [x] **7. `sd-status` shows the row, in its own section.** The issues
      section cannot carry it: `issues_section` (`source:bin/sd-status::issues_section`)
      returns `no GitHub remote` at `bin/sd-status:1197-1199` before any
      database is opened, `_database_issues` (`source:bin/sd-status::_database_issues`) closes
      its connection before `_render_issues` (`source:bin/sd-status::_render_issues`) runs,
      and the heading says `this repo, from the index`. Neither function is
      touched. A new producer `jira_section()` opens its own read-only
      connection when `sd_db.default_path()` exists — the same gate
      `_database_issues` uses, and the same `available: False, reason`
      shape when it does not — and calls `tracker_items(connection,
      tracker="jira", state=None)` with no `repo`, and
      `tracker_freshness(connection, "jira")`, and drops every closed row
      whose `last_seen` is more than seven days old before it returns, so
      the cutoff is a property of the `rows` list and not of one renderer.
      It is added to the status dict beside `"issues"` at
      `bin/sd-status:3061` under the key `jira`, so `--json` carries it. A
      new `_render_jira` prints the heading `jira (shared database, all
      repositories)` after the issues section, and `render()`
      (`source:bin/sd-status::render`) calls it — `_render_jira(result["jira"],
      write)` on the line after `_render_issues(result["issues"], write)`
      at `bin/sd-status:3328` — because a renderer that is defined and not
      called leaves the section in `--json` only:
      the freshness line, keyed on `last_success_at` and not on the state
      word because `tracker_freshness` says `degraded`, not `never`, once a
      failed heartbeat exists (design, "Readers") — `never collected`, with
      ` (<reason>)` when the latest heartbeat carries one, else the
      `external context` pair; then every open row as
      `KEY  open  <title>` with `KEY` = `row["url"].rpartition("/")[2]` and
      `<title>` = `json.dumps(row["title"], ensure_ascii=False)`, the rule
      `_render_contributions` applies to external text at
      `bin/sd-status:3365-3368`; then every closed row the producer kept as
      `KEY  closed  <title>`; then `none` if there were no rows. Nothing
      formats `number` and nothing slices a title. `ORDER`
      (`source:tests/test_sd_status.py::ORDER`) gains the heading after
      `issues (this repo, from the index)`, and the skeleton test's count
      moves from thirteen to fourteen. Every hand-built result the suite
      passes to `render()` gains a `jira` entry, starting with
      `report` on `ReportSectionTests` (`source:tests/test_sd_status.py::ReportSectionTests`),
      which builds `issues` and `contributions` and no `jira` — without
      that edit the existing skeleton tests fail with `KeyError` before
      any new case runs. `skills/sd-status/SKILL.md` is rewritten in three
      places, not appended to: `:23` ("The thirteen sections") and `:29`
      ("the thirteen below") say fourteen; `:41`'s issues row keeps saying
      "for this repository", and a new row for the `jira` section says it
      is the operator's Jira involvement from the shared database, across
      every repository, and is not scoped to the checkout.
      Verify, in `tests/test_sd_status.py`: (a) a fixture database holding
      one `jira` row with `number NULL` and `url .../browse/LOG-23818`
      renders `LOG-23818  open  "Benchmark harness"` under the new heading in
      a checkout **with no GitHub remote**, and raises nothing — the
      regression for the `:<6` format at `bin/sd-status:3394`, which raises
      `TypeError` on `None` today, and the proof the section does not sit
      behind the slug gate; (b) the same database with no `jira` rows and
      no heartbeat renders `never collected` then `none`; (c) a
      `tracker-sync:jira` heartbeat with `ok False` and a reason, no
      watermark, and two stored rows — the partial-collect fixture —
      renders `never collected (<reason>)` **and** both rows, because a
      truncated first run stores what it saw; (d) a closed row with
      `last_seen` eight days old is absent from **both** the text and
      `jira_section()["rows"]`, and the same row at six days prints as
      `closed` and is in the list — the cutoff asserted on the producer, so
      `--json` cannot carry what the text hides; (e) a row whose title is
      `"a\x1b[2Jb\nprotection"` prints as one line under the `jira`
      heading, quoted, and the skeleton order test still counts fourteen
      headings — the control-character fixture, the same shape
      `_render_contributions` is tested against; (f) `--json` output carries
      a `jira` object with `available`, `reason`, `freshness` and `rows`,
      and each row carries `url`, `state`, `title` and `last_seen`, asserted
      on the parsed JSON and not on the text; (g) the issues section's
      existing assertions are unchanged, including `no GitHub remote` in a
      checkout without one — the control. Mutation: route the Jira row
      through `_render_issues` and (a) reddens with the `TypeError`; gate
      `jira_section` on the slug and (a) reddens on the missing heading;
      delete the `_render_jira` call from `render()` and (a) reddens on the
      missing heading while (f) stays green, which is why both exist; move
      the seven-day filter into `_render_jira` and (d) reddens on the
      `rows` assertion.
      *Done 2026-09-15 on pack branch
      `fix/sd-361-step-7-sd-status-jira-section`: `jira_section()` and
      `_render_jira` in `bin/sd-status`, `JiraSectionTests` in
      `tests/test_sd_status.py` carrying (a) to (g), the skeleton test
      counting fourteen, and the four mutations each reddened as written.*

- [x] **7b. The dashboard shows the key.** `where` (in `dashboard/app.js`)
      returns `issue.url.split("/").pop()` when `number` is null and the
      row has a URL, and `issue.repo || issue.tracker` only when it does
      not; the comment above it, which says the identity is in the URL
      tail, is already the rationale. The page stays an open worklist
      (`dashboard/server.py` (line 657 as of sd:361, retired at sd:719 step 4)); nothing else in `dashboard/` changes.
      Verify: a test in `tests/test_dashboard_now.py`, source-reading like
      that file's `fillIssues` cases, asserts the null-number branch of
      `where` derives from `issue.url` and not from `issue.repo` first; and
      `python -m unittest tests.test_loc_caps` stays green with
      `DASHBOARD_CODE_CAP` (in `tests/test_loc_caps.py`, retired at sd:719 step 7) unmoved. Mutation:
      restore `issue.repo || issue.tracker` as the first branch and the
      test reddens. Manual check after step 2 has run: the issues tab shows
      `LOG-23929`, linked, where before this step it showed `LOG`. The key is
      step 2 (d)'s, not the seeded `LOG-23818`, which no collect returns.
      *Done 2026-09-15 on the same branch: `where` reads the URL tail first,
      the source-reading test in `tests/test_dashboard_now.py` reddens on
      the restored branch, and `DASHBOARD_CODE_CAP` did not move. The
      manual check after step 8 is the owner's.*

- [x] **8. The first real sync, and the measurement.** With steps 1 to 7
      landed and the venv reinstalled at the new pin, run
      `bin/sd shadow sync --strict` once by hand.
      Use the calling convention (`AGENTS.md:49-61`), then re-run the measurement
      block in `design.md` and record the numbers on sd:361 as a note.
      Verify: the run prints `shadow sync[jira]: wrote N shadow row(s)` and
      `shadow sync[jira]: cursor moved to cover from <stamp>`; `select
      tracker, count(*) from shadow group by tracker` returns two rows where
      today it returns one; `select key from state where kind='watermark'
      group by key` returns `github` and `jira`; `select url, state from
      shadow where tracker='jira' and url like '%/browse/LOG-23929'` returns
      one row — step 2 (d)'s key, inside `FIRST_RUN_WINDOW`, rather than the
      seeded `LOG-23818`, which the window excludes, so the query can answer
      at all; `bin/sd-status` in any checkout, with or without a GitHub
      remote, prints `LOG-23929` under `jira (shared database, all
      repositories)`. The nightly's next run, read from its log the following
      morning, carries both prefixes.
      *Done, measured 2026-09-16, note 2522 on sd:361: the nightly's 08:27
      UTC run on the pinned library was the first real Jira sync, its log
      carrying `shadow sync[jira]: wrote 12 shadow row(s)` and `cursor moved
      to cover from 2026-06-18T08:27:41Z`; the hand run by path at 14:54
      UTC, `bin/sd shadow sync --strict`, exited 0 and printed both prefixes
      (github 11 rows, jira 0). The measurement block: `shadow` by tracker
      `github` 3931 and `jira` 12, two rows where there was one; watermark
      keys `github` and `jira`; the `LOG-23929` browse URL one row, open,
      last seen `2026-09-16T08:27:42+00:00`; `bin/sd-status` prints
      `LOG-23929 open` under `jira (shared database, all repositories)`,
      last successful sync `2026-09-16T14:54:07Z`.*

- [x] **9. Correct the item.** The PRD's last acceptance line: sd:361's body
      still says it builds an iteration and per-tracker watermarks that
      exist. Add a note to the item, and edit issue #805's "What to build"
      into the migration this document describes, citing `design.md`, and
      restate its fifth acceptance line the way design.md's "The dashboard"
      reads it: `sd-status` shows a collected ticket with its key and state;
      the dashboard shows the key and, as an open worklist, shows the ticket
      while it is open. Name `LOG-23929` there, not the seeded `LOG-23818`,
      which `FIRST_RUN_WINDOW` excludes — step 2 (d).
      Verify: `bin/sd store item 361` shows the note; the issue body no
      longer contains the sentence "The caller iterates trackers" as a thing
      to build, and its fifth acceptance line names the two readers
      separately.
      *Done 2026-09-16, note 2522 on sd:361: issue #805's "What to build" is
      now "What was built", the migration `design.md` describes, and its
      fifth acceptance line names the two readers separately with
      `LOG-23929`; the body no longer says "The caller iterates trackers" as
      a thing to build.*

Steps 1 and 2 are the operator's and gate everything after them. Steps 3 and
4 are one pull request in `platypeeps/system`; step 5 is one here; steps 6,
7 and 7b may share a pull request here; steps 8 and 9 follow the merge.

## Verification

**Named before the work starts.**

- The per-tracker watermark rule is proved as a fact about the `state`
  table, not about a return value: after a Jira sync, `github`'s cursor row
  count is unchanged and `jira`'s is one; after a GitHub sync on the same
  database, `jira`'s is unchanged. Step 4 (a) and its mirror.
- Every guard is proved by mutation, restored, and `diff -q` reports the
  tree identical. A mutation script asserts its own edit applied
  (`assert text.count(old) == 1`) before running the test, because a pattern
  that matches nothing reports a false pass.
- The pack half is run twice: once at the pinned library, once at the new
  one. Both green is the claim; one green is not.
- No value of any `JIRA_*` variable appears in any log, test output, note, or
  pull request body. Presence only. `p=JIRA_API_TOKEN; grep -rn "${p}=" .`
  over the pack and the system checkout prints nothing — the pattern is
  assembled from a shell variable because a document that spelled it out
  would match itself; today it prints nothing, and
  the fixture at line 426 of `tests/test_sd_dashboard_index.py` at `2a2dbad6` is the shape a test
  value takes — a dict literal with a placeholder, never an assignment.
- The acceptance lines on the item map to steps: line 1 to step 8, line 2
  to step 6 (b), line 3 to a second sync after a ticket closes — not
  provable on demand; recorded on the item when it first happens — line 4
  to step 6 (c), line 5 to steps 2, 7 and 7b — `sd-status` shows key and
  state; the dashboard shows the key and, being an open worklist, the
  state only as presence (design, "The dashboard"), which step 9 records
  on the item — line 6 to step 4's grep.

**What cannot be verified from the repository, stated rather than invented:**
that the token authenticates the operator's own account — step 2 (c) puts
that question to Jira directly rather than inferring it from a row that is
absent for any of several reasons, and it answered the operator's own
accountId; that any collected ticket is still open (either answer is correct
and the row says which); and that the nightly job's environment carries the
variables, which only the morning-after log shows.
