#!/usr/bin/env bash
#
# Double-click this file (macOS opens .command files in Terminal), or run
# ./start.command from a shell. Either way it sets everything up if needed and
# opens the dashboard in your browser.
#
# It does the four steps you'd otherwise do by hand: cd here, make the venv,
# install the requirements, start the server. All four are skipped if they're
# already done, so the second run is just the last one.
#
# Any arguments are passed straight through to the script, so
# `./start.command --interval 30` works.

set -euo pipefail

# Double-clicking runs this from your home directory, not the project, so
# always resolve paths relative to this file rather than the working directory.
cd "$(dirname "${BASH_SOURCE[0]}")"

VENV="venv"
STAMP="$VENV/.requirements-installed"

say() { printf '\033[1m%s\033[0m\n' "$*"; }
fail() { printf '\033[1;31m%s\033[0m\n' "$*" >&2; }

# ── 1. find a usable python ─────────────────────────────────────────────
PY=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 &&
     "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    PY="$candidate"
    break
  fi
done
if [ -z "$PY" ]; then
  fail "Python 3.10+ not found."
  echo
  echo "  macOS:  brew install python   (or python.org/downloads)"
  echo "  Linux:  sudo apt install python3 python3-venv"
  echo
  read -r -p "Press Return to close." _
  exit 1
fi

# ── 2. virtual environment ──────────────────────────────────────────────
if [ ! -d "$VENV" ]; then
  say "First run — creating a virtual environment in ./$VENV"
  "$PY" -m venv "$VENV"
fi

# ── 3. dependencies ─────────────────────────────────────────────────────
# Keyed on requirements.txt's checksum, so editing that file reinstalls and
# nothing else does.
WANT="$(cksum requirements.txt | awk '{print $1, $2}')"
if [ ! -f "$STAMP" ] || [ "$(cat "$STAMP")" != "$WANT" ]; then
  say "Installing dependencies (once — this takes a minute)"
  # Upgrade pip first, every time we're about to install rather than only on a
  # venv we just made. A venv built by an older Python (or by an IDE) can carry
  # a pip too old for the interpreter it's running under, which fails with
  # "module 'pkgutil' has no attribute 'ImpImporter'" — pip's own code using
  # something removed in Python 3.12. That's a confirmed failure on a real venv
  # here, and it's the one error that would greet someone who already had a
  # venv before this launcher existed. Cheap: this block almost never runs.
  "$VENV/bin/python" -m pip install --quiet --upgrade pip
  "$VENV/bin/python" -m pip install --quiet -r requirements.txt
  printf '%s' "$WANT" > "$STAMP"
fi

# ── 4. go ───────────────────────────────────────────────────────────────
say "Starting — the dashboard will open in your browser."
echo "The first scrape takes about a minute. Press Ctrl+C here to stop."
echo
set +e
"$VENV/bin/python" sssb_kth_monitor.py "$@"
STATUS=$?
set -e

# Double-clicked, the Terminal window closes the moment this exits and takes
# the error message with it. Hold it open so a crash is actually readable.
if [ "$STATUS" -ne 0 ] && [ "$STATUS" -ne 130 ]; then
  echo
  fail "Exited with status $STATUS — the error should be above."
  read -r -p "Press Return to close." _
fi
