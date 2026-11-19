@echo off
chcp 65001 >nul
rem 路径由 scripts/paths.py 解析、launcher 导出；单独双击时退回脚本所在目录与默认值
if not defined NAL_HOME set "NAL_HOME=%~dp0.."
if not defined NAL_VENV_PY set "NAL_VENV_PY=%NAL_HOME%\.venv\Scripts\python.exe"
if not defined NAL_PORT_ADMIN set "NAL_PORT_ADMIN=6780"
cd /d "%NAL_HOME%"
title NAL Bot
rem 群号留空 = 所有群都服务：省掉 --group，别让 argparse 把下一个参数当成群号
set "GROUP_ARG="
if defined NAL_GROUP_ID set "GROUP_ARG=--group %NAL_GROUP_ID%"
set "ADMIN_ARG="
if defined NAL_ADMIN_ID set "ADMIN_ARG=--admin %NAL_ADMIN_ID%"
rem 云端 API 端点档位（默认千问官方；已显式设置时不覆盖）
if not defined QWEN_PROFILE set "QWEN_PROFILE=dashscope"
"%NAL_VENV_PY%" scripts\qq_bridge.py %GROUP_ARG% %ADMIN_ARG% --admin-port %NAL_PORT_ADMIN%
