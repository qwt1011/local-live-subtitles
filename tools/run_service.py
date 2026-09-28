"""一键启动流式字幕服务：先做预检，再把服务拉起来。

为什么要有这个脚本：直接用 `python -m app.server` 有两个常见坑——

1. **必须在项目目录下运行**。在别处跑会得到
   `ModuleNotFoundError: No module named 'app'`，而这个报错不会告诉你"该 cd 到哪"。
2. 模型缺失、端口被占用时，报错来自很深的地方（onnxruntime / OSError），
   不如在启动前就把话说清楚。

配套的 `启动字幕服务.bat` 是纯 ASCII 的薄壳，只负责切目录和调用本脚本——
之所以不把中文写进 .bat，是因为 cmd.exe 按当前代码页逐字节解析批处理，
UTF-8 中文会把语句拆碎（实测会把 echo 的中文当成命令去执行）。
"""

import argparse
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 启动这个组合：SenseVoice 识别 + Qwen 指令模型翻译
REQUIRED = {
    "sensevoice-2024": "识别模型",
    "Qwen2.5-0.5B-Instruct": "翻译模型",
}


def check_models():
    """检查模型是否就绪，返回缺失的条目名。"""
    from app.models_catalog import CATALOG, MODELS_DIR, is_ready

    print("[1/3] 检查模型")
    print(f"      模型目录：{MODELS_DIR}")
    missing = []
    for name, label in REQUIRED.items():
        entry = CATALOG.get(name)
        if entry is None:
            continue
        ready = is_ready(entry)
        print(f"      {label}（{name}）：{'已就绪' if ready else '缺失'}")
        if not ready:
            missing.append(name)

    # 翻译模型的 CT2 版是转换产物，不在 catalog 的必需文件里，单独看
    ct2 = MODELS_DIR / "Qwen2.5-0.5B-Instruct-ct2-int8" / "model.bin"
    print(f"      翻译模型 CT2 版：{'已就绪' if ct2.is_file() else '缺失'}")
    if not ct2.is_file():
        missing.append("Qwen2.5-0.5B-Instruct-ct2-int8")

    for name in missing:
        if name.startswith("Qwen"):
            print(f"      → 缺它不影响启动，只是不会显示中文。安装：")
            print(f"        python tools/setup_models.py --engine qwen2.5-0.5b-instruct")
            print(f"        ct2-transformers-converter --model models/Qwen2.5-0.5B-Instruct "
                  f"--output_dir models/Qwen2.5-0.5B-Instruct-ct2-int8 --quantization int8 --force")
        else:
            print(f"      → 安装：python tools/setup_models.py --engine {name}")
    return missing


def check_port(host, port):
    print(f"[2/3] 检查端口 {port}")
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            print(f"      [错误] 端口 {port} 已被占用——很可能已经有一个服务在跑。")
            print(f"             先关掉那个窗口，或查出并结束它：")
            print(f'             netstat -ano | findstr ":{port}"')
            print(f"             taskkill /PID <上面查到的PID> /F")
            return False
    print("      端口空闲")
    return True


def main():
    parser = argparse.ArgumentParser(description="一键启动本地实时字幕服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--no-translate", action="store_true", help="只出原文，不加载翻译模型")
    parser.add_argument("--language", default="ja")
    args = parser.parse_args()

    print("=" * 60)
    print("  本地实时字幕 - 流式服务")
    print("=" * 60)
    print()

    check_models()
    if not check_port(args.host, args.port):
        return 1

    print()
    print("[3/3] 启动服务" + ("" if args.no_translate else "（带翻译）"))
    print()
    print("-" * 60)
    print("  保持这个窗口开着。扩展点了「开始捕获」后，这里会打印：")
    print("    - 客户端已连接          → 扩展连上服务了")
    print("    - 采集开始：language=ja → 扩展真的在推音频了")
    print("    - 若 3 秒收不到音频会有明确警告，并指出该查哪里")
    print("  按 Ctrl+C 停止。")
    print("-" * 60)
    print()

    argv = ["app.server", "--engine", "sensevoice", "--model", "sensevoice-2024",
            "--host", args.host, "--port", str(args.port), "--language", args.language]
    if not args.no_translate:
        argv.append("--translate")
    sys.argv = argv

    from app.server import main as server_main
    try:
        server_main()
    except KeyboardInterrupt:
        print("\n已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
