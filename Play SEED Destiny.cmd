@echo off
pushd "%~dp0"
where pythonw >nul 2>&1
if errorlevel 1 (
  echo Python 3.11 or newer is required. Install Python or use the packaged launcher.
  pause
  popd
  exit /b 1
)
start "" pythonw "%~dp0launch.py"
popd
