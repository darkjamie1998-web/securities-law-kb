@echo off
cd /d "%~dp0"

echo ================================================
echo   环境自检（便携版 / 开发版通用）
echo ================================================
echo.

set PY=.venv\Scripts\python.exe
if exist "runtime\python.exe" set PY=runtime\python.exe

echo [1/3] Python 运行时: %PY%
if not exist "%PY%" (
    echo   [失败] 未找到 Python 运行时
    goto fail
)
%PY% -c "import sys; print('   Python', sys.version.split()[0], 'OK')"
if errorlevel 1 goto fail
%PY% -c "import fastapi, bs4, httpx, yaml, lxml; print('   核心依赖导入 OK')"
if errorlevel 1 (
    echo   [失败] 依赖导入异常
    goto fail
)
echo.

echo [2/3] 知识库数据库
if not exist "data\knowledge.db" (
    echo   [失败] 未找到 data\knowledge.db
    goto fail
)
%PY% -c "import sqlite3; n = sqlite3.connect(r'data\knowledge.db').execute('select count(*) from laws').fetchone()[0]; print('   knowledge.db OK，法规数:', n)"
if errorlevel 1 (
    echo   [失败] 数据库打开异常
    goto fail
)
echo.

echo [3/3] 大模型配置
if exist "data\config.json" (
    echo   data\config.json 已存在
) else (
    echo   [提示] 未配置：复制 config.example.json 为 data\config.json 并填写
    echo         未配置时关键词检索可用，语义检索/LLM 问答不可用
)
echo.
echo ================================================
echo   自检完成，双击 启动面板.bat 启动服务
echo ================================================
pause
exit /b 0

:fail
echo.
echo 自检未通过，请阅读 README.md 排查。
pause
exit /b 1
