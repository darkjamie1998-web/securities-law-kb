# tools/gen_bats.py — 生成 GBK+CRLF 编码的 bat 启动件（项目规范：cmd 原生代码页，勿用会写 UTF-8 的工具直接编辑 bat）
# 用法: python tools/gen_bats.py
#
# 环境探测优先级（三个 bat 一致）：
#   runtime\python.exe（便携版）> .venv\Scripts\python.exe（开发版/自动配置版）
#   两者皆无 → 环境自检.bat 用系统 Python（py -3 / python / python3）跑 tools/setup_env.py
#   自动创建项目内 .venv（绝不改系统 PATH / 不装全局包）
import os

BATS = {}

BATS["启动面板.bat"] = r'''@echo off
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
'''

BATS["环境自检.bat"] = r'''@echo off
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
'''

BATS["relations.bat"] = r'''@echo off
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
'''


def main():
    for name, content in BATS.items():
        # 规范: GBK 编码 + CRLF 行尾；确保 CRLF（源串统一先归一）
        text = content.replace("\r\n", "\n").replace("\n", "\r\n")
        with open(name, "wb") as f:
            f.write(text.encode("gbk"))
        print(f"written {name} ({len(text.encode('gbk'))} bytes, GBK+CRLF)")


if __name__ == "__main__":
    os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    main()
