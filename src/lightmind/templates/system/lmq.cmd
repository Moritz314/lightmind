@echo off
rem Quick chat: same as "lm q" (no start menu; this device, this folder, defaults of config.md "quick:").
call "%~dp0lm.cmd" q %*
exit /b %ERRORLEVEL%
