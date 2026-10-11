@echo off
chcp 65001 >nul
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
set "PYTHON_EXE=C:\Users\thc1\AppData\Local\Python\pythoncore-3.14-64\python.exe"

if not exist "%PYTHON_EXE%" (
    echo ERROR: Required Python runtime was not found: "%PYTHON_EXE%"
    exit /b 9009
)

cd /d C:\Fund_Server\Code\US_singlestock_complete_pipeline
"%PYTHON_EXE%" -X utf8 run_single_stock_pipeline.py %* >> C:\Fund_Server\Logs\us_single_stock_pipeline.log 2>&1
