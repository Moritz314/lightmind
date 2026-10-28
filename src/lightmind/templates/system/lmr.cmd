@echo off
rem Resume a chat: same as "lm resume" (a list of the last chats, or a name or id, or --last).
call "%~dp0lm.cmd" resume %*
exit /b %ERRORLEVEL%
