@echo off
set "SCRIPT=%~dp0split_pdf_by_chapter.py"
set "BUNDLED_PYTHON=%USERPROFILE%\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"

if exist "%BUNDLED_PYTHON%" goto run_bundled
where python >nul 2>&1
if %ERRORLEVEL%==0 goto run_system

echo Python was not found. Install Python 3.10 or newer, then run: python -m pip install -r requirements.txt
pause
exit /b 1

:run_bundled
"%BUNDLED_PYTHON%" "%SCRIPT%"
goto finish

:run_system
python "%SCRIPT%"

:finish
if not %ERRORLEVEL%==0 pause
