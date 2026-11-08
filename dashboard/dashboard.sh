#!/bin/sh
# The command pack owns the workflow dashboard (sd:5987): Today, Backlog, Writing
# and item controls share sd_db. The <prefix>.sd-dashboard LaunchAgent
# (prefix from SYSTEM_TOOLS_LABEL_PREFIX, default local.system-tools) manages its loopback
# server and optional authenticated Tailscale IP listener.
# The legacy collectors back the machine and vault views: Management, Notes and
# Tasks run sd_tile.py for Toolbox, Research, Vault and Queues (pack sd:7527),
# and Health's Ports calls `collectors.collect_ports` in-process.
# `tile` runs one view from a shell.
# Usage: dashboard.sh tile <tab> | serve | preflight | install | health | shots | koi-sync | pages | test
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"

# DASHBOARD_PYTHON (.env) is the interpreter for `tile`, the subcommand a
# person runs from a shell, and for nothing else. `serve`
# runs under SD_DASHBOARD_PYTHON (below), and a Resources view runs the tile as
# a child of that server under its own sys.executable — so the same tile has
# two interpreters, today Homebrew python@3.15 on this path and Homebrew
# python@3.13 on that one. That matters because the vault is under ~/Documents
# behind Full Disk Access, macOS grants FDA to a binary, and each path needs
# its own grant. A tile under an ungranted interpreter does not report an empty
# vault: `collectors.vault_blocked` probes the read and the tab fails naming
# the interpreter to grant. Homebrew python is the pin here because the tidier
# one — /usr/bin/python3, whose path survives brew upgrades — does not work:
# user FDA grants for Xcode's python are silently ignored (tested 2026-08-29;
# bundle, inner binary, toggled, TCC-reset — every variant fails). The file is
# gitignored for a reason that has outlived its cause: it still carries the
# Jira credentials the deleted server's Issues tab read, which nothing here
# reads any more. `.env.example` no longer documents them.
# The file lives in the config directory: <config>/project-dashboard/.env,
# where <config> is $SYSTEM_TOOLS_CONFIG (default ~/.config/system).
#
# The three helpers below are copies of the system checkout's lib/config.sh
# functions of the same names, so the pack's launcher reads the operator's
# config by the same rule without that checkout: change them together.
SYSTEM_TOOLS_CONFIG="${SYSTEM_TOOLS_CONFIG:-${XDG_CONFIG_HOME:-$HOME/.config}/system}"

# st_shared <NAME>: a set variable wins, an empty one too; else
# <config>/shared.env, read in a subshell; else nothing.
st_shared() {
    eval "_st_set=\${$1+x}"
    if [ -n "$_st_set" ]; then
        unset _st_set
        eval "printf '%s\n' \"\$$1\""
        return 0
    fi
    unset _st_set
    [ -f "$SYSTEM_TOOLS_CONFIG/shared.env" ] || return 0
    _st_out=$(
        . "$SYSTEM_TOOLS_CONFIG/shared.env" >/dev/null 2>&1 </dev/null || exit 1
        eval "printf '%s\n' \"\${$1-}\""
    ) || {
        printf 'st_shared: %s does not source; its values are ignored\n' "$SYSTEM_TOOLS_CONFIG/shared.env" >&2
        unset _st_out
        return 0
    }
    printf '%s\n' "$_st_out"
    unset _st_out
}

# st_label_prefix: SYSTEM_TOOLS_LABEL_PREFIX, else local.system-tools.
st_label_prefix() {
    _st_prefix=$(st_shared SYSTEM_TOOLS_LABEL_PREFIX)
    printf '%s\n' "${_st_prefix:-local.system-tools}"
    unset _st_prefix
}

# st_source_env <tool>: source <config>/<tool>/.env with `set -a` when it
# exists; a value in the environment wins over the file, an empty one too.
st_source_env() {
    _st_env="$SYSTEM_TOOLS_CONFIG/$1/.env"
    if [ -f "$_st_env" ]; then
        _st_n=0
        for _st_name in $(grep -o -E '[A-Za-z_][A-Za-z0-9_]*:?=' "$_st_env" | sed 's/:*=$//'); do
            if printenv "$_st_name" >/dev/null 2>&1; then
                _st_val=$(printenv "$_st_name")
                _st_n=$((_st_n + 1))
                eval "_st_name_$_st_n=\$_st_name; _st_val_$_st_n=\$_st_val"
            fi
        done
        set -a
        # shellcheck disable=SC1090
        . "$_st_env"
        set +a
        while [ "$_st_n" -gt 0 ]; do
            eval "_st_name=\$_st_name_$_st_n"
            eval "$_st_name=\$_st_val_$_st_n; export $_st_name"
            unset "_st_name_$_st_n" "_st_val_$_st_n"
            _st_n=$((_st_n - 1))
        done
        unset _st_n _st_name _st_val
    fi
    unset _st_env
}

st_source_env project-dashboard
# The checkout root and the JEV_JOB_TRIAGE stage switch may come from .env. Export the ones set
# so collectors.py and `install` (which copies them into the LaunchAgent) see
# the same values this script does.
for _name in REPO_ROOT SYSTEM_TOOLS_CONFIG JEV_JOB_TRIAGE; do
  # shellcheck disable=SC2163 # exports the variable `_name` names, on purpose
  if eval "[ -n \"\${$_name:-}\" ]"; then export "$_name"; fi
done
# The label prefix and the vault are kept once, in <config>/shared.env (sd:3990).
# runtime.py reads the prefix from the environment, and `sd store` the vault.
SYSTEM_TOOLS_LABEL_PREFIX="$(st_label_prefix)"; export SYSTEM_TOOLS_LABEL_PREFIX
OBSIDIAN_VAULT="$(st_shared OBSIDIAN_VAULT)"
if [ -n "$OBSIDIAN_VAULT" ]; then export OBSIDIAN_VAULT; else unset OBSIDIAN_VAULT; fi

cmd_tile() {
  # One tab per invocation, JSON on stdout. The five seconds and 64KB are the
  # *outer* ceilings a caller applies to the whole child: the pack's loader
  # applied them, a Resources view applies them now
  # (`reports_screen.VIEW_SECONDS`), and a run from here has neither. The
  # inner ones still hold on this path -- `sd_tile.main` sets a deadline of
  # `TILE_SECONDS - TILE_MARGIN`, and each collector reads inside
  # `collectors.BUDGET_BYTES`. A subcommand rather than a second script because
  # .env is sourced above and names the interpreter this path's vault reads
  # need.
  #
  # Exit codes follow the retired plugin contract, not this script: 1 when a
  # collector failed, with the reason on stderr, and 2 when the tab name is
  # wrong. A Resources view shows that reason for any nonzero exit. Nothing
  # but the payload may reach stdout, so this execs.
  exec "${DASHBOARD_PYTHON:-python3}" "$DIR/sd_tile.py" "$@"
}

# sd_db is the lib/ of the pack checkout this folder sits in (sd:2997,
# sd:5987), here and in the test verb. -I ignores the caller's PYTHONPATH and
# working directory; the bootstrap adds only this dashboard package and that
# lib/, and the server refuses any other copy, an installed one included
# (sd:3279).
SD_DB="$DIR/../lib"
# The server's interpreter, and so the one the tiles a Resources view runs
# inherit. One default, read by `serve`, `preflight`, `install`, `health` and
# `grants`, so the path `grants` probes is the path `serve` would run.
RUNTIME_PYTHON_DEFAULT="$HOME/repos/platypeeps/sd-workflow/.venv/bin/python"

cmd_grants() {
  # Both interpreters the vault is read under (the comment at the top), each
  # probed as the binary whose grant is in question, one line per path. This
  # is the report `collectors.vault_blocked` cannot give: it names the
  # interpreter a tile runs under, on the tile that was asked, and a grant the
  # other path lost -- `brew upgrade python` moves that Cellar and drops it --
  # had nowhere to appear but as an empty tab (sd:831). Exit 0 when both list
  # the vault and the answers are their own -- the control was refused, or it
  # was still waiting when its timeout ran out, which is what a read nobody
  # can grant looks like, so it counts as one kept out (sd:845) -- 1 when one
  # cannot (the line says what to grant), 3 when they are not the binaries'
  # answers: /bin/ls, which holds no grant of its own, listed the vault too,
  # so this shell's own Documents access reached both and an ok proves
  # nothing -- or the control gave no conclusive result, which is every other
  # end it can come to: it never started, the call on it failed, or it came
  # back nonzero saying something that is not an access refusal. Coming back
  # unsettled and running out the clock are not one answer: only the second
  # carries the lines above it. From a Terminal the leak is the usual case,
  # and the reason .claude/rules/macos-tcc.md says to test a grant from launchd, not a terminal.
  # Driven by the server's interpreter, as `preflight` and `health` are; the
  # suspect paths are arguments, so a broken one is a line and not a crash.
  if [ "$#" -gt 0 ]; then
    echo "usage: $(basename "$0") grants (no arguments; the interpreters come from .env and the environment)" >&2
    exit 2
  fi
  runtime_python="${SD_DASHBOARD_PYTHON:-$RUNTIME_PYTHON_DEFAULT}"
  if [ ! -x "$runtime_python" ]; then
    echo "dashboard: missing installed runtime $runtime_python; set SD_DASHBOARD_PYTHON to the server's interpreter" >&2
    exit 1
  fi
  # -I, as `serve` and `runtime` below: this answers which binary holds a
  # grant, and a caller's PYTHONPATH or user site-packages could change what
  # the probe runs. sd_tile.py imports the standard library only and loads
  # collectors.py by path, so isolation costs it no import.
  exec "$runtime_python" -I "$DIR/sd_tile.py" --grants \
    "DASHBOARD_PYTHON=${DASHBOARD_PYTHON:-python3}" \
    "SD_DASHBOARD_PYTHON=$runtime_python"
}

cmd_serve() {
  runtime_python="${SD_DASHBOARD_PYTHON:-$RUNTIME_PYTHON_DEFAULT}"
  if [ ! -x "$runtime_python" ]; then
    echo "dashboard: missing installed runtime $runtime_python; run the pack's setup or set SD_DASHBOARD_PYTHON to its provisioned interpreter" >&2
    exit 1
  fi
  exec "$runtime_python" -I "$DIR/sd_dashboard/bootstrap.py" server "$@"
}

cmd_runtime() {
  runtime_python="${SD_DASHBOARD_PYTHON:-$RUNTIME_PYTHON_DEFAULT}"
  if [ ! -x "$runtime_python" ]; then
    echo "dashboard: missing installed runtime $runtime_python" >&2
    exit 1
  fi
  exec "$runtime_python" -I "$DIR/sd_dashboard/bootstrap.py" runtime "$@"
}

cmd_shots() {
  # Screenshots for the lead's review before enqueue (sd:6007): this checkout,
  # served on a copy of the database, each page dark and light at desktop and
  # phone width. Under the server's interpreter, so the pages are this build's.
  runtime_python="${SD_DASHBOARD_PYTHON:-$RUNTIME_PYTHON_DEFAULT}"
  if [ ! -x "$runtime_python" ]; then
    echo "dashboard: missing installed runtime $runtime_python" >&2
    exit 1
  fi
  exec "$runtime_python" -I "$DIR/sd_dashboard/bootstrap.py" shots "$@"
}

cmd_koi_sync() {
  # One Koi water-change sync (sd:8077), for the daily job that keeps Home
  # Assistant's changes past its recorder's ten days: `home_koi.main`.
  runtime_python="${SD_DASHBOARD_PYTHON:-$RUNTIME_PYTHON_DEFAULT}"
  if [ ! -x "$runtime_python" ]; then
    echo "dashboard: missing installed runtime $runtime_python" >&2
    exit 1
  fi
  exec "$runtime_python" -I "$DIR/sd_dashboard/bootstrap.py" home_koi "$@"
}

cmd_docs() {
  # The design documents: docs/design/*.md rendered into docs/design's sibling
  # docs/dashboard/, which `documents.py` already serves for any checkout that
  # holds one -- so this repository needs no registration step, only a build.
  # Plain python3: the renderer carries its own markdown subset rather than a
  # dependency, because no interpreter on this machine ships python-markdown
  # and the CI job installs no extras.
  exec "${PYTHON:-python3}" "$DIR/design_docs.py" "$@"
}

cmd_pages() {
  # The rail and palette map, built from the page registry in sd_dashboard/v2/pages/ (sd:2418): the one list of which
  # section opens a new page, which opens its old screen, and which old screens the palette offers. The README points
  # here instead of keeping a table that each port had to edit.
  if [ "$#" -gt 0 ]; then
    echo "usage: $(basename "$0") pages (no arguments)" >&2
    exit 2
  fi
  PYTHONPATH="$DIR${PYTHONPATH:+:$PYTHONPATH}" exec "${PYTHON:-python3}" -m sd_dashboard.v2
}

cmd_test() {
  # From this folder: `python -m` puts the working directory first on the
  # path, and the pack root's own tests/ would shadow this folder's.
  cd "$DIR"
  PYTHONPATH="$DIR:$SD_DB:$DIR/tests${PYTHONPATH:+:$PYTHONPATH}" \
    exec "${PYTHON:-python3}" -m unittest discover -s "$DIR/tests" -t "$DIR" "$@"
}

case "${1:-}" in
  tile) shift; cmd_tile "$@" ;;
  serve) shift; cmd_serve "$@" ;;
  preflight|install|health) cmd_runtime "$@" ;;
  grants) shift; cmd_grants "$@" ;;
  shots) shift; cmd_shots "$@" ;;
  koi-sync) shift; cmd_koi_sync "$@" ;;
  docs) shift; cmd_docs "$@" ;;
  pages) shift; cmd_pages "$@" ;;
  test|check) shift; cmd_test "$@" ;;
  -h|--help|help)
    cat <<'HELPEOF'
usage: dashboard.sh tile <tab> | serve | preflight | install | health | grants | shots | koi-sync | docs | pages | test

  tile <tab>       one tab as JSON on stdout — toolbox, vault, research,
                   ports or queues. The machine and vault views are
                   toolbox, vault, research and queues, and each
                   reads its tab this way; Ports has no view and reads
                   collect_ports in-process. Exits 1 with the reason on
                   stderr when a collector fails and 2 when the tab name is
                   not one of those
  serve            Today, Backlog, Writing and item controls.
                   All reads/writes use `sd_db`. Binds loopback; optional ip_origin
                   adds a listener on this node's Tailscale IP with peer authentication.
                   --config, --port and --database select explicit settings
  preflight        read config, the pack's library, database, LaunchAgent and
                   Tailscale Serve; print a reviewable plan and fingerprint
  install          preview by default. --apply --expected-fingerprint SHA
                   replaces the LaunchAgent with backups and health checks;
                   local configuration makes no Tailscale calls. Private :8443
                   routing requires an explicit HTTPS origin and operator login;
                   ip_origin optionally enables HTTP access on the Tailscale IP :8768
  health           check the configured front door and backend /health
                   once; --wait SECONDS retries until it answers healthy.
                   Prints the commit it serves, and exits 1 when that is not
                   the checkout's HEAD, naming the kickstart line, or when
                   the installed LaunchAgent's PATH no longer finds sd
  koi-sync         one Koi water-change sync from Home Assistant into the
                   pond's log, for a daily job; prints {added}, exits 1
                   naming a refusal
  grants           probe the vault under DASHBOARD_PYTHON and under
                   SD_DASHBOARD_PYTHON, one line per path, naming the one
                   that lacks Full Disk Access. Takes no arguments. Exits 1
                   when a path cannot read, 3 when the answers were not
                   measured as the binaries' own -- this shell's Documents
                   access reached them, or the control gave no conclusive
                   result -- and 0 when both hold the grant, or when the
                   control was still waiting at its timeout, which is what a
                   read nobody can grant looks like. Run it from launchd,
                   where a child's access is its own, for a conclusive 0 when
                   both hold the grant
  shots            --out DIR PAGE... draws each page (such as /tasks)
                   dark and light, 1440 and 375 wide, from this checkout
                   served on a copy of the database; prints one PNG path
                   per line. --database names the store to copy. Needs
                   Chrome (CHROME names its binary). Exits 1 naming a page
                   that did not answer 200 or did not settle
  docs             render docs/design/*.md into docs/dashboard/, diagrams
                   included, where the Documents tab already serves them.
                   --check lists stale outputs and exits 1 if any; --hashes
                   prints the CSP digests the diagram pages need, which
                   documents.py pins, and exits 1 unless the pages carry
                   exactly the pinned scripts, each on every page. Exits 3
                   when this checkout has no docs/design/
  pages            the rail and palette map from the page registry, one
                   tab-separated line per target: section, address and
                   `new` with its sd item for a registered page; `classic`
                   for a section that opens its old screen; `palette` for
                   an old screen the palette offers. Takes no arguments
  test             the dashboard's own tests. `check` is the same thing

The workflow server replaces the earlier pack dashboard on :8767. Progress
controls save to the shared database without status commits; the legacy plugin
collector commands remain available.

environment:
  DASHBOARD_PYTHON            interpreter for `tile` only
                              (.env; Homebrew python, not /usr/bin/python3).
                              It does not select the tiles a Resources view
                              renders -- those run under the server's
                              interpreter, below
  OBSIDIAN_VAULT              vault path (default ~/Documents/Obsidian Vault;
                              set it once in <config>/shared.env)
  REPO_ROOT                   checkout root, read by collectors.py itself
                              (default ~/repos; set it in
                              <config>/project-dashboard/.env)
  SYSTEM_TOOLS_CONFIG         config root (default ~/.config/system);
                              .env and documents.conf are read from its
                              project-dashboard/, cron job files from
                              cron-jobs/jobs
  SYSTEM_TOOLS_ROOT           the system checkout, which holds the machine
                              tools the dashboard runs and reads (default
                              ~/repos/system). sd_db is always the lib/ of
                              the pack checkout this script sits in
  SD_DASHBOARD_PYTHON         interpreter for
                              serve, preflight, install and health (default
                              .venv/bin/python in the sd-workflow checkout,
                              ~/repos/platypeeps/sd-workflow). A Resources
                              view runs sd_tile.py as a child of the server, so
                              this is the interpreter its vault reads use. Full
                              Disk Access is granted to a binary, so this one and
                              DASHBOARD_PYTHON each need their own grant;
                              `grants` probes both
HELPEOF
    exit 0
    ;;
  *)
    echo "usage: $(basename "$0") tile <tab> | serve | preflight | install | health | grants | shots | koi-sync | docs | pages | test" >&2
    exit 1
    ;;
esac
