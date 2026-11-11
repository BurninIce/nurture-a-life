@echo off
chcp 65001 >nul
rem 路径由 scripts/paths.py 解析、launcher 导出；单独双击时退回本机默认安装位置
if not defined NAL_NAPCAT_HOME set "NAL_NAPCAT_HOME=C:\NapCat\Shell"
if not defined NAL_SELF_ID set "NAL_SELF_ID=10000000"
cd /d "%NAL_NAPCAT_HOME%"
title NapCat - QQ %NAL_SELF_ID%
rem NapCat logs Chinese in UTF-8, but its C++ launcher resets the console
rem code page back to GBK(936). Force UTF-8 via PowerShell before launching.
rem Quick login takes the uin as a POSITIONAL arg (Usage: <exe> <quickLogin>).
rem The launcher itself builds `QQ.exe --enable-logging -q <uin>`; passing -q
rem here makes it treat the literal "-q" as the uin and fall back to the QR code.
powershell -NoProfile -Command "$OutputEncoding = [Console]::OutputEncoding = [Text.Encoding]::UTF8; & '.\NapCatWinBootMain.exe' %NAL_SELF_ID%"
