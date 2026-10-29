@echo off
rem usbip-win vhci 驱动卸载(右键"以管理员身份运行")
rem 结果写入本目录 uninstall_log.txt
rem
rem 说明:本脚本必须与 v0.3.6-dev\ 目录同级,它靠 %~dp0 相对定位。
cd /d "%~dp0v0.3.6-dev"
usbip.exe uninstall > "%~dp0uninstall_log.txt" 2>&1
echo exitcode=%ERRORLEVEL% >> "%~dp0uninstall_log.txt"
echo.
echo 卸载结束,结果见 uninstall_log.txt
pause
