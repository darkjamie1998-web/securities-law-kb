@echo off
cd /d "%~dp0"

REM python console output as GBK (cmd codepage), else CJK prints garbled
set PYTHONIOENCODING=gbk

echo ================================================
echo   证券法律法规知识库
echo   启动中... 服务地址 http://localhost:8000
echo   关闭本窗口即停止服务
echo ================================================
echo.

set PY=.venv\Scripts\python.exe
if exist "runtime\python.exe" set PY=runtime\python.exe

if not exist "%PY%" (
    echo [提示] 未找到本地运行时，转入环境自检/自动配置...
    echo.
    call "环境自检.bat"
)

REM re-detect after autocfg (env may now have .venv)
set PY=.venv\Scripts\python.exe
if exist "runtime\python.exe" set PY=runtime\python.exe

if not exist "%PY%" (
    echo [错误] 环境仍未就绪，请按上方自检提示处理后重新双击本脚本
    pause
    exit /b 1
)

if not exist "data\knowledge.db" (
    echo [提示] 未找到 data\knowledge.db，检索功能需要知识库数据库
    echo        服务仍将启动，但浏览内容为空。请阅读 README.md
    echo.
)

if not exist "data\config.json" (
    echo [提示] 未配置大模型：关键词检索可用，语义检索/问答不可用
    echo        复制 config.example.json 为 data\config.json 并填写，或页面右上齿轮配置
    echo.
)

REM delay 4s then open browser (ASCII comment only)
start "" /min cmd /c "timeout /t 4 /nobreak >NUL & start http://localhost:8000"

%PY% -m uvicorn app.main:app --port 8000

echo.
echo 服务已停止。
pause
