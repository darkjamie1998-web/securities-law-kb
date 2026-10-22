# build_portable.py — 构建全离线自包含便携包
# 用法: python build_portable.py [--skip-zip]
#
# 输出: <项目>/dist/法律法规知识库查询\  + 同级 法律法规知识库查询.zip（分发产物统一放 dist/）
# 结构: 项目源码(git 已提交状态) + runtime\(完整 Python 3.14.5 安装 + venv site-packages 合并) + data/knowledge.db
import os
import shutil
import sqlite3
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path

SRC = Path(__file__).resolve().parent
# 构建产物固定输出到项目 dist/ 下（2026-09-29 定：分发产物统一从 dist/ 取）
DST = SRC / "dist" / "法律法规知识库查询"
VENV_SITE = SRC / ".venv" / "Lib" / "site-packages"

# 项目内随包文件/目录（相对 SRC）；docs/PORTABLE-README.md 特殊处理为 README.md
# .git 默认不打入分发包（体积大且含历史），需要时用 --with-git
COPY_DIRS = ["app", "crawler", "web", "tests", "docs", "tools"]
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


def python_home() -> Path:
    """便携运行时来源：已有 runtime 优先，否则用当前解释器的 base_prefix。"""
    runtime = SRC / "runtime" / "python.exe"
    if runtime.exists():
        return SRC / "runtime"
    return Path(sys.base_prefix)


def build_db_snapshot(src_db: Path, dest_db: Path) -> Path:
    """用 SQLite backup API 生成一致性快照（服务运行时也安全）。"""
    dest_db.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(str(src_db))
    dst = sqlite3.connect(str(dest_db))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    return dest_db


def _force_rmtree(path: Path):
    """rmtree，自动清掉只读属性再删（git objects 文件默认只读，Windows 下 unlink 会拒绝访问）"""
    import stat

    def onexc(func, p, exc):
        os.chmod(p, stat.S_IWRITE)
        func(p)

    shutil.rmtree(path, onexc=onexc)


def main():
    skip_zip = "--skip-zip" in sys.argv
    with_git = "--with-git" in sys.argv
    if with_git:
        COPY_DIRS.append(".git")
    if not (SRC / "app" / "main.py").exists():
        sys.exit(f"源目录不对: {SRC}")
    py_home = python_home()
    if not py_home.exists():
        sys.exit(f"Python 安装不存在: {py_home}")
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

    print("[3/6] 复制数据: data/knowledge.db（一致性快照）")
    (DST / "data").mkdir(exist_ok=True)
    build_db_snapshot(SRC / "data" / "knowledge.db", DST / "data" / "knowledge.db")

    print("[4/6] 组装便携运行时 runtime/ (完整 Python 安装 + venv site-packages)")
    rc(py_home, DST / "runtime")
    print(f"   Python 安装 {py_home} -> runtime/")
    rc(VENV_SITE, DST / "runtime" / "Lib" / "site-packages")
    print("   venv site-packages 合并 -> runtime/Lib/site-packages")

    print("[5/6] 验证")
    py = DST / "runtime" / "python.exe"
    v = subprocess.run([str(py), "-c", "import fastapi, bs4, httpx, yaml, lxml, uvicorn, jieba; import sys; print('deps OK on', sys.version.split()[0])"],
                       capture_output=True, text=True, cwd=str(DST))
    print("  ", v.stdout.strip() or v.stderr.strip())
    if v.returncode != 0:
        sys.exit("依赖导入验证失败")
    v = subprocess.run([str(py), "-c", "import sqlite3; c=sqlite3.connect(r'data/knowledge.db'); n=c.execute('select count(*) from laws').fetchone()[0]; s=c.execute('select count(*) from articles_seg').fetchone()[0]; print('db OK, laws =', n, ', seg =', s)"],
                       capture_output=True, text=True, cwd=str(DST))
    print("  ", v.stdout.strip() or v.stderr.strip())
    if v.returncode != 0:
        sys.exit("数据库验证失败")

    print("[6/6] 统计大小")
    total = sum(f.stat().st_size for f in DST.rglob("*") if f.is_file())
    print(f"   便携包总大小: {total/1024/1024:.0f} MB")
    if not skip_zip:
        zip_path = DST.parent / f"法律法规知识库查询-{datetime.now():%Y%m%d}.zip"
        print(f"   压缩中 -> {zip_path}")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for p in sorted(DST.rglob("*")):
                if p.is_file():
                    zf.write(p, p.relative_to(DST.parent))
        print(f"   zip 大小: {zip_path.stat().st_size/1024/1024:.0f} MB")
    print("完成。")


if __name__ == "__main__":
    main()
