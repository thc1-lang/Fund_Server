@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

cd /d C:\Fund_Server\Code\us_complete_pipeline

python -X utf8 run_us_pipeline.py %* >> C:\Fund_Server\Logs\us_complete_pipeline.log 2>&1