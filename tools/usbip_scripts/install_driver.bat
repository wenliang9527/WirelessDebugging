@echo off
rem usbip-win vhci 驱动安装(右键"以管理员身份运行")
rem 结果写入本目录 install_log.txt
rem
rem 说明:本脚本必须与 v0.3.6-dev\ 目录同级,它靠 %~dp0 相对定位。
cd /d "%~dp0v0.3.6-dev"
usbip.exe install > "%~dp0install_log.txt" 2>&1
echo exitcode=%ERRORLEVEL% >> "%~dp0install_log.txt"
echo.
echo 安装结束,结果见 install_log.txt
if %ERRORLEVEL%==0 (echo 驱动安装成功) else (echo 安装失败,请查看 install_log.txt)
pause
