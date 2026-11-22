#!/usr/bin/env python3
"""Native Prism/Gito/Codex output fixture selected by the executable filename."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

config = json.loads(Path(__file__).with_name("provider-config.json").read_text())
provider = Path(sys.argv[0]).name
log = Path(config["log"])
log.parent.mkdir(parents=True, exist_ok=True)
with log.open("a", encoding="utf-8") as stream:
    stream.write(f"{provider} {' '.join(sys.argv[1:])}\n")

if provider == "prism":
    if config["prismMode"] == "invalid":
        print("human finding that must not become clean")
    elif config["prismMode"] == "clean":
        print(json.dumps({"findings": []}))
    else:
        print(
            json.dumps(
                {
                    "findings": [
                        {
                            "severity": "medium",
                            "category": "correctness",
                            "title": "Prism finding",
                            "locations": [
                                {
                                    "path": "src/app.py",
                                    "lines": {"start": 2, "end": 2},
                                }
                            ],
                        }
                    ]
                }
            )
        )
elif provider == "gito":
    if config.get("gitoMoveHead"):
        # An empty commit moves HEAD and leaves the tree clean, which is the
        # one shape the dirtiness re-check cannot see.
        subprocess.run(
            ["git", "commit", "--allow-empty", "-q", "-m", "moved mid-review"],
            check=True,
        )
    try:
        output = Path(sys.argv[sys.argv.index("--out") + 1])
    except (ValueError, IndexError):
        raise SystemExit("gito fixture requires --out <path>") from None
    try:
        output.mkdir(parents=True, exist_ok=True)
    except (FileExistsError, NotADirectoryError):
        raise SystemExit("gito fixture --out path is not a directory") from None
    count = config["gitoCount"]
    issues = [
        {
            "title": "Gito finding" if count == 1 else f"Gito finding {index + 1}",
            "details": "details",
            "severity": config["gitoSeverity"],
            "tags": ["correctness"],
            "affected_lines": [{"start_line": index + 1}],
        }
        for index in range(count)
    ]
    report = {"total_issues": count, "issues": {"src/app.py": issues}}
    (output / "code-review-report.json").write_text(
        json.dumps(report), encoding="utf-8"
    )
elif provider == "codex":
    try:
        answer = Path(sys.argv[sys.argv.index("--output-last-message") + 1])
        schema = Path(sys.argv[sys.argv.index("--output-schema") + 1])
    except (ValueError, IndexError):
        raise SystemExit(
            "codex fixture requires --output-schema and --output-last-message"
        ) from None
    if not schema.is_file():
        raise SystemExit("codex fixture expects the schema file to be seeded")
    mode = config.get("codexMode", "finding")
    if mode == "logged-out":
        print("error: not logged in; run codex login", file=sys.stderr)
        raise SystemExit(1)
    if mode == "mutate":
        repo = Path(sys.argv[sys.argv.index("-C") + 1])
        (repo / "src/app.py").write_text("seed\nchanged\ndrifted\n", encoding="utf-8")
        mode = "clean"
    if mode == "invalid":
        answer.write_text("not json", encoding="utf-8")
    else:
        findings = (
            []
            if mode == "clean"
            else [
                {
                    "path": "src/app.py",
                    "line": 2,
                    "severity": "high",
                    "evidence": "python3 src/app.py: Traceback",
                    "summary": "Codex finding",
                    "family": "correctness",
                }
            ]
        )
        answer.write_text(json.dumps({"findings": findings}), encoding="utf-8")
else:
    raise SystemExit(f"unsupported fixture provider: {provider}")
