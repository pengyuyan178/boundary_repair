"""无需安装的本目录入口；不修改父项目或 baseline 的 Python 搜索路径。"""
import sys
from pathlib import Path


def main() -> int:
    """输入命令行参数；将本包 src 加入路径后调用统一 CLI，返回退出码。"""
    sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
    from boundary_repair.cli import main as cli_main
    return cli_main()


if __name__ == "__main__":
    raise SystemExit(main())
