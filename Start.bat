@echo off
setlocal
cd /d "%~dp0"

py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>&1
if not errorlevel 1 (
    py -3 start.py %*
    goto finished
)

python -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" >nul 2>&1
if not errorlevel 1 (
    python start.py %*
    goto finished
)

echo Python 3.12 or newer is required.
echo Install it from https://www.python.org/downloads/ with the Python launcher or PATH option enabled.
echo Then double-click Start.bat again.
pause
exit /b 1

:finished
if errorlevel 1 (
    echo.
    echo The application could not start. See the error above.
    pause
    exit /b 1
)
exit /b 0
