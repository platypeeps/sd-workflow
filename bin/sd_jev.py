"""What the Jev shadow stages in `bin/sd_jev_shadow.py` share.

The review-tier reading (`JEV_SD_REVIEW`) and its hint-blind twin
(`JEV_SD_REVIEW_BLIND`) are gone (sd:8972): shadow data from 2026-10-24 showed
no sign of value, so `sd-review` reviews at the tier `sd_route` picks.

`jev` lives in a private companion repository and is absent on most machines.
Absence is the ordinary case and not a fault, so it is silent; `jev enabled`
exiting 3 is the same not-configured case. Any other failure is loud on stderr.
"""

from __future__ import annotations

import pathlib
import subprocess
from typing import Any, Mapping, Sequence, TextIO

#: Resolved on the ``PATH`` of the environment the run was handed, never from a
#: path written down here: this repository is public and has no fixed relative
#: path to a private companion.
COMMAND = "jev"
#: This lane's name in the judgment ledger (sd:1253).
CALLER = "sd-review"

#: Every call is bounded: a run that hangs is a fault and not slow progress.
TIMEOUT_SECONDS = 20


def _jev_run(argv: Sequence[str], env: Mapping[str, str],
             state: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run one bounded command, turning every way it can fail into an exit code.
    Bytes that do not decode are replaced, never raised (review of 46529bae2)."""

    try:
        return subprocess.run(list(argv), env=dict(env), input=state, capture_output=True,
                              text=True, errors="replace", timeout=TIMEOUT_SECONDS, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        return subprocess.CompletedProcess(list(argv), 1, "", str(error))


def shadow_stopped(stream: TextIO, caller: str, stage: str, why: str) -> None:
    stream.write(f"{caller}: the Jev shadow reading stopped: {why}; nothing it does "
                 f"changed (set {stage}=0 to stop asking)\n")


def jev_triage(findings: Sequence[Mapping[str, Any]], env: Mapping[str, str],
               root: str | pathlib.Path, stream: TextIO | None = None) -> None:
    """Shadow review triage (sd:2092): `bin/sd_jev_shadow.py`, outside the review lane's line cap."""

    import sd_jev_shadow  # noqa: PLC0415 -- it imports this module

    sd_jev_shadow.record_triage(findings, env, root, stream)
