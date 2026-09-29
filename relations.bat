@echo off
cd /d "%~dp0"

REM python console output as GBK (cmd codepage), else CJK prints garbled
set PYTHONIOENCODING=gbk

echo ================================================
echo   关联图谱全量生成（独立窗口运行）
echo   进度日志: data\relations_forever.log
echo   已抽取过的法规自动跳过，可随时中断重跑
echo ================================================
echo.

set PY=.venv\Scripts\python.exe
if exist "runtime\python.exe" set PY=runtime\python.exe

if not exist "%PY%" (
    echo [错误] 未找到本地运行时 runtime\ 或 .venv\
    echo        请先双击 环境自检.bat 完成环境配置
    pause
    exit /b 1
)

REM single source of logic: run_relations_forever.py
REM (instance lock + watchdog + retry cap), output to log file (overwrite per run)
REM watchdog exit code 3 = gateway hang / no progress -> auto restart after pause

:run
%PY% run_relations_forever.py --batch 50 > data\relations_forever.log 2>&1
if errorlevel 3 (
    echo.
    echo [watchdog] runner 退出（网关挂起或长时间无进展），10 分钟后自动重启...
    echo 完全停止请直接关闭本窗口。
    timeout /t 600 /nobreak >NUL
    goto run
)

echo.
echo 已完成或手动停止。详见 data\relations_forever.log
pause
