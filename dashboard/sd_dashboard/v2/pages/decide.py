"""Decide (sd:5781): what waits on a ruling, in two views. Decisions (sd:3012, Today's view until sd:5781) lists each open
question note: `/api/decisions` reads them and `/api/decisions/answer` records a ruling (`decisions`); under them the
last week's rulings group by session, and `/api/decisions/reverse` reopens the question a ruling answered (pack sd:6015). Scoring (sd:3709,
the Review page until sd:5781) is the labelling queue: `/api/review` lists the open batches and their open questions; a
label posts `/api/review/rule` and its undo `/api/review/withdraw`; a batch's labels download from
`/api/review/<item>/export`. Each is `sd_db.review`, the code `sd-db review` runs (`review_screen`). `/review`, the old
address, serves this page, which opens it on Scoring with its query (decide.js)."""

from __future__ import annotations

from . import Api, Page, attention

EXPORT = r"/api/review/([1-9][0-9]{0,18})/export"


def _decisions(read):
    from ... import decisions, review_screen

    found = decisions.document(read.connection)
    # The rail badge (sd:3446): the decisions waiting and the questions open to score, the counts on the page's two tabs.
    waiting, scoring = len(found["decisions"]), review_screen.document(read.connection)["attention"]["n"]
    what = " and ".join(word for count, word in ((waiting, "decisions waiting"), (scoring, "questions open")) if count)
    found["attention"] = attention(["caution"] * (waiting + scoring), {"caution": what or "decisions or questions"})
    return found


def _answer(payload, principal):
    from ... import decisions

    return decisions.request(payload)


def _reverse(payload, principal):
    from ... import decisions

    return decisions.reverse_request(payload)


def _read(read):
    from sd_db import operations

    from ... import review_screen

    # The weekly advisory sweep (sd:9505) rides on Scoring's read, not the badge's: only the page reads its log.
    # The server passes no jobs backend; launchd's names the logs folder, as on every sibling page.
    jobs = read.jobs or operations.LaunchdBackend()
    return {**review_screen.document(read.connection),
            "sweeps": review_screen.sweeps(read.connection, getattr(jobs, "cron_root", None))}


def _export(read):
    import re

    from ... import review_screen

    item = int(re.fullmatch(EXPORT, read.path)[1])
    if item > review_screen.MAX_ID:
        return 400, {"error": "No such batch."}
    return review_screen.export(read.connection, item)


def _rule(payload, principal):
    from ... import review_screen

    return review_screen.rule_request(payload)


def _withdraw(payload, principal):
    from ... import review_screen

    return review_screen.withdraw_request(payload)


PAGE = Page(
    section="Decide",
    routes=("/decide", "/review"),
    html="decide.html",
    item="sd:5781",
    api=(Api("/api/decisions", _decisions), Api("/api/decisions/answer", write=_answer),
         Api("/api/decisions/reverse", write=_reverse),
         Api("/api/review", _read), Api(pattern=EXPORT, example="/api/review/7/export", read=_export),
         Api("/api/review/rule", write=_rule), Api("/api/review/withdraw", write=_withdraw)),
    count="/api/decisions",
    live=True,
)
