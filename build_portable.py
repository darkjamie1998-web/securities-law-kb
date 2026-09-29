# build_portable.py — 构建全离线自包含便携包
# 用法: python build_portable.py [--skip-zip]
#
# 输出: <项目>/dist/法律法规知识库查询\  + 同级 法律法规知识库查询.zip（分发产物统一放 dist/）
# 结构: 项目源码(git 已提交状态) + runtime\(完整 Python 3.14.5 安装 + venv site-packages 合并) + data/knowledge.db
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

SRC = Path(r"D:\workmate_projects\法律法规知识库查询").resolve()
# 构建产物固定输出到项目 dist/ 下（2026-09-29 定：分发产物统一从 dist/ 取）
DST = SRC / "dist" / "法律法规知识库查询"
PY_HOME = Path(r"C:\Program Files\Python314")
VENV_SITE = SRC / ".venv" / "Lib" / "site-packages"

# 项目内随包文件/目录（相对 SRC）；docs/PORTABLE-README.md 特殊处理为 README.md
COPY_DIRS = ["app", "crawler", "web", "tests", "docs", "tools", ".git"]
COPY_FILES = [
    ".gitignore", ".gitattributes", "AGENTS.md", "CLAUDE.md",
    "config.example.json", "requirements-full.txt", "requirements.txt",
    "启动面板.bat", "环境自检.bat", "relations.bat", "run_relations_forever.py",
]
XD = ["__pycache__", ".pytest_cache"]           # robocopy 排除目录
XF = ["*.pyc", "*.log"]


def rc(src: Path, dst: Path, *extra):
    """robocopy 包装；返回码 <8 均视为成功（1=有文件复制, 0=无变化）"""
    dst.mkdir(parents=True, exist_ok=True)
    args = ["robocopy", str(src), str(dst), "/E", "/MT:16",
            "/NFL", "/NDL", "/NJH", "/NJS", "/NP",
            "/XD", *XD, "/XF", *XF, *extra]
    r = subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if r.returncode >= 8:
        raise RuntimeError(f"robocopy 失败 {src} -> {dst} (code={r.returncode})\n{r.stdout[-2000:]}")
    return r.returncode


def _force_rmtree(path: Path):
    """rmtree，自动清掉只读属性再删（git objects 文件默认只读，Windows 下 unlink 会拒绝访问）"""
    import stat

    def onexc(func, p, exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)

    shutil.rmtree(path, onexc=onexc)


def main():
    skip_zip = "--skip-zip" in sys.argv
    if not (SRC / "app" / "main.py").exists():
        sys.exit(f"源目录不对: {SRC}")
    if not PY_HOME.exists():
        sys.exit(f"Python 安装不存在: {PY_HOME}")
    if not VENV_SITE.exists():
        sys.exit(f"venv site-packages 不存在: {VENV_SITE}")

    print(f"[1/6] 清理目标目录 {DST}")
    if DST.exists():
        _force_rmtree(DST)
    DST.mkdir(parents=True)

    print("[2/6] 复制项目文件（git 已提交状态的工作区）")
    for d in COPY_DIRS:
        rc(SRC / d, DST / d)
        print(f"   {d}/")
    for f in COPY_FILES:
        shutil.copy2(SRC / f, DST / f)
    shutil.copy2(SRC / "docs" / "PORTABLE-README.md", DST / "README.md")  # 便携版 README 源
    print("   根文件 + README.md (from docs/PORTABLE-README.md)")

    print("[3/6] 复制数据: data/knowledge.db")
    (DST / "data").mkdir(exist_ok=True)
    shutil.copy2(SRC / "data" / "knowledge.db", DST / "data" / "knowledge.db")

    print("[4/6] 组装便携运行时 runtime/ (完整 Python 安装 + venv site-packages)")
    rc(PY_HOME, DST / "runtime")
    print("   Python 3.14.5 完整安装 -> runtime/")
    rc(VENV_SITE, DST / "runtime" / "Lib" / "site-packages")
    print("   venv site-packages 合并 -> runtime/Lib/site-packages")

    print("[5/6] 验证")
    py = DST / "runtime" / "python.exe"
    v = subprocess.run([str(py), "-c", "import fastapi, bs4, httpx, yaml, lxml, uvicorn; import sys; print('deps OK on', sys.version.split()[0])"],
                       capture_output=True, text=True, cwd=str(DST))
    print("  ", v.stdout.strip() or v.stderr.strip())
    if v.returncode != 0:
        sys.exit("依赖导入验证失败")
    v = subprocess.run([str(py), "-c", "import sqlite3; n = sqlite3.connect(r'data/knowledge.db').execute('select count(*) from laws').fetchone()[0]; print('db OK, laws =', n)"],
                       capture_output=True, text=True, cwd=str(DST))
    print("  ", v.stdout.strip() or v.stderr.strip())
    if v.returncode != 0:
        sys.exit("数据库验证失败")

    print("[6/6] 统计大小")
    total = sum(f.stat().st_size for f in DST.rglob("*") if f.is_file())
    print(f"   便携包总大小: {total/1024/1024:.0f} MB")
    if not skip_zip:
        zip_path = DST.parent / (DST.name + ".zip")
        print(f"   压缩中 -> {zip_path}")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for p in sorted(DST.rglob("*")):
                if p.is_file():
                    zf.write(p, p.relative_to(DST.parent))
        print(f"   zip 大小: {zip_path.stat().st_size/1024/1024:.0f} MB")
    print("完成。")


if __name__ == "__main__":
    main()
