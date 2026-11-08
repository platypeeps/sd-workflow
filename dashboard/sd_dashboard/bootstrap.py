"""Isolated internal entry point; dashboard.sh selects the interpreter."""

import runpy
import sys
from pathlib import Path

# -I ignores PYTHONPATH and the caller's working directory. This dashboard
# package goes on the path, then the command pack's lib/ ahead of it, so
# `sd_db` is the pack's and never a copy installed in this interpreter
# (sd:3279).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sd_dashboard import pack_library  # noqa: E402 - needs the path above

sys.path.insert(0, str(pack_library()))
module = sys.argv.pop(1)
if module not in ("server", "runtime", "shots", "home_koi"):
    raise SystemExit("unknown dashboard module")
runpy.run_module(f"sd_dashboard.{module}", run_name="__main__")
