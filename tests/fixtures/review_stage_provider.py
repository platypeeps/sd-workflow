#!/usr/bin/env python3
"""Deterministic provider fixture for the exact-scope review-stage tests."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

provider, artifact, log_value, mode = sys.argv[1:]
log = Path(log_value)
Path(artifact).mkdir(parents=True, exist_ok=True)
log.parent.mkdir(parents=True, exist_ok=True)
with log.open("a", encoding="utf-8") as stream:
    stream.write(f"{provider}:start:{time.monotonic()}\n")
if mode == "barrier":
    barrier = log.parent / f"{log.name}.{provider}.ready"
    barrier.touch()
    deadline = time.monotonic() + 2
    starts = 0
    while time.monotonic() < deadline:
        starts = len(list(log.parent.glob(f"{log.name}.*.ready")))
        if starts >= 2:
            break
        time.sleep(0.02)
    if starts < 2:
        raise SystemExit("parallel start barrier timed out")
else:
    time.sleep(2 if mode == "slow" else 0.35)
with log.open("a", encoding="utf-8") as stream:
    stream.write(f"{provider}:end:{time.monotonic()}\n")
if mode == "finding":
    print(
        json.dumps(
            {
                "status": "findings",
                "findings": [
                    {
                        "path": "src/app.py",
                        "line": 2,
                        "severity": "high", "instances": ["src.py:1"],
                        "evidence": "python3 src/app.py: Traceback",
                        "summary": "validate state",
                        "family": "boundary-validation",
                    }
                ],
            }
        )
    )
elif mode == "finding-alt":
    print(
        json.dumps(
            {
                "status": "findings",
                "findings": [
                    {
                        "path": "src/app.py",
                        "line": 2,
                        "severity": "low",
                        "summary": "validate state",
                        "family": "security",
                    }
                ],
            }
        )
    )
elif mode.startswith("severity-"):
    # Severity-parameterised findings for the advisory-ceiling gate. The
    # summary differs per mode so the derived stable id differs too, which
    # keeps a receipt cached under one mode from being reused under another.
    label = mode.removeprefix("severity-")
    finding = {
        "path": "src/app.py",
        "line": 2,
        "summary": f"{label} observation",
        "family": "boundary-validation",
    }
    if label != "unspecified":
        finding["severity"] = label
    print(json.dumps({"status": "findings", "findings": [finding]}))
elif mode == "markdown-fenced-quotes":
    # PR #353: a Markdown-only diff whose fenced blocks quote source. The
    # provider read the quotations as the diff's own code and re-reported the
    # very defects the document was filing. Three findings, at the three fences.
    print(
        json.dumps(
            {
                "status": "findings",
                "findings": [
                    {
                        "path": "docs/prd.md",
                        "line": line,
                        "severity": "high", "instances": ["src.py:1"],
                        "evidence": "python3 src/app.py: Traceback",
                        "summary": summary,
                        "family": "boundary-validation",
                    }
                    for line, summary in (
                        (21, "session id is not validated before use"),
                        (54, "duplicate entry is appended without a check"),
                        (94, "journal write is not atomic"),
                    )
                ],
            }
        )
    )
elif mode == "markdown-hallucinated-typo":
    # PR #353 again, after the fences were retagged to `text`: a fourth finding
    # citing a misspelling that is not at the cited line, or anywhere else.
    print(
        json.dumps(
            {
                "status": "findings",
                "findings": [
                    {
                        "path": "docs/prd.md",
                        "line": 140,
                        "severity": "low",
                        "summary": "Typographical error: 'descision' should be 'decision'",
                        "family": "contract-documentation-drift",
                    }
                ],
            }
        )
    )
elif mode == "mixed-severity":
    print(
        json.dumps(
            {
                "status": "findings",
                "findings": [
                    {
                        "path": "src/app.py",
                        "line": 2,
                        "severity": "low",
                        "summary": "mixed advisory observation",
                        "family": "boundary-validation",
                    },
                    {
                        "path": "src/app.py",
                        "line": 3,
                        "severity": "high", "instances": ["src.py:1"],
                        "evidence": "python3 src/app.py: Traceback",
                        "summary": "mixed real defect",
                        "family": "boundary-validation",
                    },
                ],
            }
        )
    )
elif mode == "finding-whitespace":
    print(
        json.dumps(
            {
                "status": "findings",
                "findings": [
                    {
                        "path": "src/app.py",
                        "line": 2,
                        "severity": "medium",
                        "summary": "validate whitespace family",
                        "family": "   ",
                    }
                ],
            }
        )
    )
elif mode in {"case-upper", "case-lower"}:
    path = "SRC/App.py" if mode == "case-upper" else "src/app.py"
    print(
        json.dumps(
            {
                "status": "findings",
                "findings": [
                    {
                        "path": path,
                        "line": 2,
                        "severity": "medium",
                        "summary": "same summary",
                    }
                ],
            }
        )
    )
elif mode == "finding-fail":
    print(
        json.dumps(
            {
                "status": "findings",
                "findings": [
                    {
                        "path": "src/app.py",
                        "line": 2,
                        "summary": "mapped clean finding",
                    }
                ],
            }
        )
    )
    raise SystemExit(9)
elif mode == "malicious-finding":
    print(
        json.dumps(
            {
                "status": "findings",
                "findings": [
                    {
                        "path": "../secret",
                        "line": True,
                        "summary": "unsafe provider fields",
                        "disposition": "fixed",
                    }
                ],
            }
        )
    )
elif mode == "fail":
    print(json.dumps({"status": "clean", "findings": []}))
    print("provider failed", file=sys.stderr)
    raise SystemExit(9)
elif mode == "rate-limit":
    print("provider rate limited", file=sys.stderr)
    raise SystemExit(8)
elif mode == "cancelled":
    print("provider cancelled", file=sys.stderr)
    raise SystemExit(10)
elif mode == "large-output":
    sys.stdout.write("x" * (5 * 1024 * 1024))
else:
    print(json.dumps({"status": "clean", "findings": []}))
