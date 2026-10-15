@echo off
cd /d %~dp0
echo Installing required parts for KENTO...
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo Install failed. Is Python installed? Please read README.md.
)
pause
