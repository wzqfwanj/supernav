# SuperNav 本地启动脚本（项目根目录执行）
# 用法：.\run.ps1
# 可选：$env:AMAP_KEY = "你的高德 Web 服务 Key"（未设置时进入演示模式）

$ErrorActionPreference = "Stop"
Set-Location -Path $PSScriptRoot

python -m uvicorn backend.app:app --host 127.0.0.1 --port 8000
