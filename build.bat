@echo off
cd /d "%~dp0"
python -m pip install --quiet pyinstaller || exit /b 1
python -m PyInstaller --onefile --windowed --name CommitMsg --noconfirm app.py
exit /b %errorlevel%
