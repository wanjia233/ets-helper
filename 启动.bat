@echo off
rem E-TingShuo homework helper launcher (ASCII only on purpose:
rem cmd.exe reads .bat as the OEM codepage, so non-ASCII breaks parsing)
cd /d "%~dp0"

where pythonw >nul 2>nul
if %errorlevel%==0 (
    start "" pythonw "ets_helper.py"
    exit /b 0
)

for %%P in (
  "%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe"
  "%LOCALAPPDATA%\Programs\Python\Python311\pythonw.exe"
  "%LOCALAPPDATA%\Programs\Python\Python310\pythonw.exe"
  "C:\Python312\pythonw.exe"
  "C:\Python311\pythonw.exe"
) do (
    if exist %%P (
        start "" %%P "ets_helper.py"
        exit /b 0
    )
)

echo pythonw.exe not found. Install Python 3 or add python to PATH.
pause
