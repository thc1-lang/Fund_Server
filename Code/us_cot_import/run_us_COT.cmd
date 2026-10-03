@echo off
setlocal
if not exist "C:\Fund_Server\Logs" mkdir "C:\Fund_Server\Logs"
>>"C:\Fund_Server\Logs\us_COT.log" echo ==== %date% %time% Start ====
"C:\Users\thc1\AppData\Local\Python\pythoncore-3.14-64\python.exe" -u -X utf8 "C:\Fund_Server\Code\us_cot_import\us_COT.py" --service-account-path "C:\Fund_Server\Code\us_complete_pipeline\google_credentials.json" %* >>"C:\Fund_Server\Logs\us_COT.log" 2>&1
set "run_exit=%ERRORLEVEL%"
>>"C:\Fund_Server\Logs\us_COT.log" echo ==== %date% %time% Exit code %run_exit% ====
exit /b %run_exit%
