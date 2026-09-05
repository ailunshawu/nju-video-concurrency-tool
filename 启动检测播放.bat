@echo off
call "%~dp0video_qa\run_live_tool.bat" video_qa.startup %*
exit /b %ERRORLEVEL%
