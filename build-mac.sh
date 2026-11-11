#!/usr/bin/env bash
# Construit CommitMsg pour macOS : dist/CommitMsg.app (et dist/CommitMsg, l'exécutable seul).
# Usage : ./build-mac.sh [--open]     (--open lance l'app une fois construite)
set -euo pipefail
cd "$(dirname "$0")"

# 1. Un Python qui a tkinter (sinon l'app plante au démarrage : "No module named tkinter")
PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  for dir in /opt/homebrew/bin /usr/local/bin ""; do
    cand="${dir:+$dir/}$c"
    if command -v "$cand" >/dev/null 2>&1 && "$cand" -c "import tkinter" >/dev/null 2>&1; then PY="$(command -v "$cand")"; break 2; fi
  done
done
if [ -z "$PY" ]; then
  echo "Aucun Python avec tkinter trouvé. Installe-le puis relance :" >&2
  echo "  brew install python-tk@3.12" >&2
  exit 1
fi
echo "Python : $PY ($("$PY" --version))"

# 2. venv local (recréé s'il n'a pas tkinter)
if [ ! -x .venv/bin/python ] || ! .venv/bin/python -c "import tkinter" >/dev/null 2>&1; then
  rm -rf .venv
  "$PY" -m venv .venv
fi
.venv/bin/python -m pip install --quiet --upgrade pip pyinstaller 2>&1 | grep -v DEPRECATION || true

# 3. Build
rm -rf build dist
.venv/bin/python -m PyInstaller --onefile --windowed --name CommitMsg --noconfirm --clean app.py >/tmp/commitmsg-build.log 2>&1 \
  || { tail -20 /tmp/commitmsg-build.log >&2; echo "Build échoué (log complet : /tmp/commitmsg-build.log)" >&2; exit 1; }

echo "OK : $(pwd)/dist/CommitMsg.app"
[ "${1:-}" = "--open" ] && open dist/CommitMsg.app
exit 0
