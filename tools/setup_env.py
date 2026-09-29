# tools/setup_env.py — 自动配置项目内 .venv 环境（被 环境自检.bat 在缺运行时时调用，也可手动运行）
# 原则：只在项目文件夹内操作（创建 .venv、依赖装入 .venv），绝不修改系统 PATH / 注册表 / 全局站点
# 用法: python tools/setup_env.py [--mirror <pip镜像URL>]
# 前提: 当前解释器 Python >= 3.9（本脚本只用标准库）
import os
import subprocess
import sys
from pathlib import Path

# bat 以 GBK 代码页控制台调用本脚本；管道/重定向时 Python 可能输出 UTF-8 导致乱码，统一按 GBK
if os.name == "nt":
    sys.stdout.reconfigure(encoding="gbk", errors="replace")
    sys.stderr.reconfigure(encoding="gbk", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
VENV_PY = ROOT / ".venv" / "Scripts" / "python.exe"
REQUIREMENTS = ROOT / "requirements.txt"
MIN_PY = (3, 9)
DEFAULT_MIRROR = "https://pypi.tuna.tsinghua.edu.cn/simple"
CORE_IMPORTS = "import fastapi, bs4, httpx, yaml, lxml"


def run(cmd, **kw):
    """跑子进程，实时透传输出；返回 returncode"""
    print("  $", " ".join(str(c) for c in cmd))
    return subprocess.run([str(c) for c in cmd], cwd=str(ROOT), **kw).returncode


def main():
    print("=" * 48)
    print("  自动配置项目环境（.venv 虚拟环境）")
    print("=" * 48)

    ver = sys.version_info
    print(f"\n[1/4] 基准解释器: {sys.executable}")
    print(f"      Python {ver.major}.{ver.minor}.{ver.micro}")
    if (ver.major, ver.minor) < MIN_PY:
        print(f"\n[失败] 需要 Python >= {MIN_PY[0]}.{MIN_PY[1]}，当前 {ver.major}.{ver.minor}")
        print("       请安装较新的 Python（推荐 3.14）后重新运行 环境自检.bat")
        return 1

    print(f"\n[2/4] 创建虚拟环境 .venv（位于项目文件夹内，不动系统）")
    if VENV_PY.exists():
        print("      .venv 已存在，复用（如需重建请先手动删除 .venv 目录）")
    else:
        code = run([sys.executable, "-m", "venv", ROOT / ".venv"])
        if code != 0 or not VENV_PY.exists():
            print("\n[失败] venv 创建失败，请把上方报错发给维护者")
            return 1
        print("      已创建 .venv")

    print(f"\n[3/4] 安装依赖到 .venv（requirements.txt，宽松版本）")
    mirror = None
    if "--mirror" in sys.argv:
        i = sys.argv.index("--mirror")
        if i + 1 < len(sys.argv):
            mirror = sys.argv[i + 1]
    # pip 自身升级（失败不阻断——可能仅源里无新版，继续装依赖）
    run([VENV_PY, "-m", "pip", "install", "--upgrade", "pip"])
    # 先用 pip 默认源配置装（内网机器若配了镜像源会自动生效）；失败再回退公共镜像
    code = run([VENV_PY, "-m", "pip", "install", "-r", REQUIREMENTS])
    if code != 0:
        fallback = mirror or DEFAULT_MIRROR
        print(f"      默认源安装失败，改用镜像重试: {fallback}")
        code = run([VENV_PY, "-m", "pip", "install", "-r", REQUIREMENTS, "-i", fallback])
    if code != 0:
        print("\n[失败] 依赖安装失败（可能无网络或内网源不可达）")
        print("       替代方案：从便携包 zip 重新解压完整 runtime\\ 目录（零网络零安装）")
        print("       或在内网机器执行: python tools/setup_env.py --mirror <内网pip镜像地址>")
        return 1

    print(f"\n[4/4] 验证核心依赖")
    code = run([VENV_PY, "-c", CORE_IMPORTS])
    if code != 0:
        print("\n[失败] 依赖导入异常")
        return 1
    print("      核心依赖导入 OK")

    print("\n" + "=" * 48)
    print("  环境配置完成（.venv 就绪）")
    print("  下一步: 双击 启动面板.bat 启动服务")
    print("  说明: 'render' 来源的增量同步首次需执行")
    print(f"        {VENV_PY} -m playwright install chromium")
    print("=" * 48)
    return 0


if __name__ == "__main__":
    sys.exit(main())
