@echo off
rem Starts Study Deck on this computer so players on the same Wi-Fi can join.
rem Double-click to run. Close this window (or press Ctrl+C) to stop it.
setlocal
cd /d "%~dp0"

rem Personal sign-in: "Connect Brightspace" uses your own Purdue sign-in on this computer
rem (see STUDY_DECK.md and POLICY_COMPLIANCE.md section 4). To turn it off, put "rem " in
rem front of the next line.
set STUDY_DECK_BROWSER_MODE_ACKNOWLEDGED=true

if not exist ".venv\Scripts\python.exe" (
  echo First run: setting up Study Deck. This takes a minute or two...
  where py >nul 2>nul && (py -3 -m venv .venv) || (python -m venv .venv)
  if errorlevel 1 goto :nopython
  ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
  ".venv\Scripts\python.exe" -m pip install --quiet -e ".[web,browser]"
  if errorlevel 1 goto :failed
)

".venv\Scripts\python.exe" -m study_deck.app
goto :eof

:nopython
echo Python 3.10 or newer is needed. Install it from https://www.python.org/downloads/ and try again.
pause
goto :eof

:failed
echo Setting up Study Deck failed. See the messages above.
pause
