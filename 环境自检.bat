@echo off
cd /d "%~dp0"

REM python console output as GBK (cmd codepage), else CJK prints garbled
set PYTHONIOENCODING=gbk

echo ================================================
echo   环境自检（便携版 / 开发版通用）
echo ================================================
echo.

:detect
set PY=
if exist "runtime\python.exe" set PY=runtime\python.exe
if exist ".venv\Scripts\python.exe" set PY=.venv\Scripts\python.exe
if not "%PY%"=="" goto checks

echo [提示] 未找到本地运行时（runtime\ 或 .venv\），尝试自动配置...
echo.
set SETUP=
where py >NUL 2>NUL
if errorlevel 1 goto try_python
set SETUP=py -3
goto autocfg

:try_python
where python >NUL 2>NUL
if errorlevel 1 goto try_python3
set SETUP=python
goto autocfg

:try_python3
where python3 >NUL 2>NUL
if errorlevel 1 goto nopy
set SETUP=python3
goto autocfg

:autocfg
echo ------------------------------------------------
echo   检测到系统 Python（%SETUP%）
echo   自动创建项目内 .venv 虚拟环境并安装依赖
echo   仅在项目文件夹内操作，不修改系统环境
echo ------------------------------------------------
%SETUP% tools\setup_env.py
if errorlevel 1 goto fail
echo.
echo [成功] .venv 配置完成，重新自检...
echo.
goto detect

:nopy
echo [失败] 本机未找到任何 Python（py / python / python3 均不可用）
echo.
echo 两条路任选其一：
echo   1. 从便携包 zip 重新解压完整 runtime\ 目录（推荐，零安装零网络）
echo   2. 安装 Python 3.9 或更高版本（推荐 3.14，安装时勾选 Add to PATH）
echo      安装完成后重新双击本脚本，会自动配置项目内 .venv
goto fail

:checks
echo [1/3] Python 运行时: %PY%
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
echo 自检未通过，请按上方提示处理，或阅读 README.md 故障排查章节。
pause
exit /b 1
