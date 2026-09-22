@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ================================================
echo   关联图谱分批生成（每批 50 部）
echo   独立窗口运行，关闭本窗口即停止
echo   进度日志: data\relations_batch.log
echo ================================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [错误] 未找到 .venv 虚拟环境
    pause
    exit /b 1
)

REM 连续分批：每批 50 部，直到剩余不足（无 pending 时脚本自动结束）
:loop
.venv\Scripts\python.exe -m app.relations --pending 50
if errorlevel 1 (
    echo.
    echo [提示] 本批出现错误，10 秒后重试下一批（已处理的会自动跳过）...
    timeout /t 10 /nobreak >nul
)
REM 检查是否还有未处理法规
.venv\Scripts\python.exe -c "import sqlite3,sys; db=sqlite3.connect('data/knowledge.db'); done=db.execute('SELECT count(DISTINCT a.law_id) FROM relations r JOIN articles a ON a.id=r.from_id').fetchone()[0]; total=db.execute('SELECT count(*) FROM laws').fetchone()[0]; print(f'进度: {done}/{total}'); sys.exit(0 if done<total else 1)"
if not errorlevel 1 goto loop

echo.
echo ================================================
echo   全部法规的关联图谱生成完成！
echo ================================================
pause
