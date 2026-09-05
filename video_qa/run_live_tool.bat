@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
pushd "%~dp0.."
if errorlevel 1 exit /b 1
if defined VIDEO_QA_PYTHON goto check_explicit
set "VIDEO_QA_PYTHON=%CD%\.venv\Scripts\python.exe"
call :probe
if "%VIDEO_QA_PROBE%"=="VIDEO_QA_READY" goto ready
set "VIDEO_QA_PYTHON=python"
:check_explicit
call :probe
if not "%VIDEO_QA_PROBE%"=="VIDEO_QA_READY" goto dependency_error
:ready
if /i "%~1"=="--self-test" goto self_test
"%VIDEO_QA_PYTHON%" -m %*
set "VIDEO_QA_EXIT=%ERRORLEVEL%"
popd
pause
exit /b %VIDEO_QA_EXIT%
:self_test
echo LIVE_QA_LAUNCHER_OK
popd
exit /b 0
:dependency_error
echo Python 3.10+ with Playwright is required.
echo Set VIDEO_QA_PYTHON to your Python executable with Playwright installed.
popd
if /i not "%~1"=="--self-test" pause
exit /b 1
:probe
set "VIDEO_QA_PROBE="
for /f "delims=" %%P in ('""%VIDEO_QA_PYTHON%" "%~dp0check_runtime.py" 2^>nul"') do set "VIDEO_QA_PROBE=%%P"
exit /b 0
