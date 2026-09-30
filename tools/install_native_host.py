"""安装 / 卸载扩展的一键启动宿主（只写当前用户注册表，不需要管理员）。

    python tools/install_native_host.py            # 安装（可重复运行：移动项目文件夹后再跑一次即可）
    python tools/install_native_host.py --uninstall

写入的东西只有两样：
1. 注册表 HKCU\\Software\\Google\\Chrome\\NativeMessagingHosts\\local.live_subtitles
   → 默认值是清单文件的路径；
2. 清单文件 runs/native_host/local.live_subtitles.json
   → 宿主 .bat 的路径 + 允许调用它的扩展 ID。

扩展 ID 由扩展文件夹路径算出（Chrome 对"加载已解压的扩展"就是这么生成 ID 的），
所以不用手动去 chrome://extensions 复制。改代码、换模型都不影响这里；只有项目文件夹搬家时要重跑。
"""

import argparse
import hashlib
import json
import sys
import winreg
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOST_NAME = "local.live_subtitles"
REG_KEY = rf"Software\Google\Chrome\NativeMessagingHosts\{HOST_NAME}"
MANIFEST = ROOT / "runs" / "native_host" / f"{HOST_NAME}.json"
HOST_BAT = ROOT / "tools" / "native_host.bat"
EXTENSION_DIR = ROOT / "extension"


def extension_id(directory):
    """Chrome 给未打包扩展算 ID 的方法：路径（UTF-16LE）的 SHA-256 前 32 个十六进制位映射到 a–p。"""
    digest = hashlib.sha256(str(directory).encode("utf-16-le")).hexdigest()[:32]
    return "".join(chr(ord("a") + int(ch, 16)) for ch in digest)


def install():
    ext_id = extension_id(EXTENSION_DIR.resolve())
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps({
        "name": HOST_NAME,
        "description": "本地实时字幕：从扩展一键启动/停止本地识别服务",
        "path": str(HOST_BAT),
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{ext_id}/"],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, REG_KEY) as key:
        winreg.SetValueEx(key, "", 0, winreg.REG_SZ, str(MANIFEST))
    print("一键启动已安装。")
    print(f"  扩展 ID：{ext_id}")
    print(f"  清单：{MANIFEST}")
    print(f"  注册表：HKCU\\{REG_KEY}")
    print("接下来：在 chrome://extensions 里点本扩展的「重新加载」，然后打开扩展弹窗即可。")


def uninstall():
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, REG_KEY)
        print(f"已删除注册表 HKCU\\{REG_KEY}")
    except FileNotFoundError:
        print("注册表项本来就不存在")
    MANIFEST.unlink(missing_ok=True)
    print("一键启动已卸载（服务本身和 .bat 启动方式不受影响）。")


def main():
    parser = argparse.ArgumentParser(description="安装/卸载扩展一键启动宿主")
    parser.add_argument("--uninstall", action="store_true")
    args = parser.parse_args()
    if sys.platform != "win32":
        raise SystemExit("目前只支持 Windows")
    uninstall() if args.uninstall else install()


if __name__ == "__main__":
    main()
