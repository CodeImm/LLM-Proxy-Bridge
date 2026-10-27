@echo off
chcp 65001 >nul 2>&1

rem Empty "" after start is required: first quoted arg is the window title.
start "" wt.exe -d "C:\Users\admin\code\proxy\CLIProxyAPI_8.0.33_windows_amd64" powershell -NoExit -Command ".\cli-proxy-api.exe" ; new-tab -d "C:\Users\admin\code\proxy" powershell -NoExit -Command "python bridge.py" ; new-tab -d "C:\Users\admin\code\keycap.pro" powershell -NoExit -Command "./opus.bat"

echo Session restored.
pause