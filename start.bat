@echo off
rem Double-click to start the app: runs the local server and opens it in the browser.
rem Keep this window open while you use the app; close it to stop.
cd /d "%~dp0"
set OPEN_BROWSER=1
set PYTHONUTF8=1
if exist "python\python.exe" (
  "python\python.exe" server.py
) else (
  where py >nul 2>nul && (py -3 server.py) || (python server.py)
)
if errorlevel 1 (
  echo.
  echo The app stopped with an error. If Python is missing, download the Windows package from the Releases page.
  pause
)
