#!/bin/bash
# Build macOS : dist/CommitMsg.app (lancer sur un Mac ; PyInstaller ne cross-compile pas)
set -e
cd "$(dirname "$0")"
python3 -m pip install --quiet pyinstaller
python3 -m PyInstaller --onefile --windowed --name CommitMsg --noconfirm app.py
echo "OK : dist/CommitMsg.app"
