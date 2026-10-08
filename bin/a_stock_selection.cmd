@echo off
rem a_stock_selection 统一启动器（Windows CMD）
setlocal
set "ROOT=%~dp0.."
if "%PYTHON%"=="" (set "PY=python") else (set "PY=%PYTHON%")
"%PY%" "%ROOT%\scripts\run.py" %*
endlocal