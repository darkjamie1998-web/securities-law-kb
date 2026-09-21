@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ================================================
echo   证券法律法规知识库
echo   启动中... 服务地址 http://localhost:8000
echo   关闭本窗口即停止服务
echo ================================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [错误] 未找到 .venv 虚拟环境，请先运行: python -m venv .venv
    pause
    exit /b 1
)

REM 延迟 4 秒后自动打开浏览器
start "" /min cmd /c "timeout /t 4 /nobreak >nul & start http://localhost:8000"

.venv\Scripts\python.exe -m uvicorn app.main:app --port 8000

echo.
echo 服务已停止。
pause
