@echo off
cd /d "%~dp0"
title Codex
set "PATH=C:\Program Files\nodejs;%APPDATA%\npm;C:\Program Files\Git\cmd;%PATH%"
for /f "usebackq tokens=1,* delims==" %%a in (".env") do if "%%a"=="API_KEY" set "RELAY_API_KEY=%%b"
git add -A >nul 2>&1
git commit -q -m "打开Codex前自动备份 %date% %time%" >nul 2>&1
echo 已自动备份当前文件（改错了可以撤回）。
echo 下面直接打字说要改什么，回车发送。退出输入 /exit
echo.
codex
pause
