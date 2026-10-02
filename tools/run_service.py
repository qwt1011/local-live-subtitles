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

# 启动这个组合：识别模型（默认 Parakeet）+ 翻译模型（默认 Hy-MT2，备选 Qwen 指令模型）
# 识别模型：名字 → (引擎, catalog 键)。parakeet 是 09-30 评测的候选（CER 0.090 → 0.066，但单次识别约慢 3 倍）。
ASR_MODEL = {
    "sensevoice": ("sensevoice", "sensevoice-2024"),
    "parakeet": ("sherpa", "parakeet-ja"),
}
# 混合模式：草稿用 SenseVoice（快），定稿用 Parakeet 重解码一次（准）。翻译只吃定稿。
HYBRID = {"hybrid": (("sensevoice", "sensevoice-2024"), ("sherpa", "parakeet-ja"))}

TRANSLATE_MODEL = {
    "hymt": "hy-mt2",
    "instruct": "qwen2.5-0.5b-instruct",
}


def check_models(engine, asr="parakeet", en_asr="parakeet"):
    """检查模型是否就绪，返回缺失的条目名。"""
    from app.models_catalog import CATALOG, MODELS_DIR, is_ready

    print("[1/3] 检查模型")
    print(f"      模型目录：{MODELS_DIR}")
    missing = []
    if asr in HYBRID:
        required = {HYBRID[asr][0][1]: "草稿识别模型", HYBRID[asr][1][1]: "定稿识别模型"}
    else:
        required = {ASR_MODEL[asr][1]: "识别模型"}
    required[TRANSLATE_MODEL[engine]] = "翻译模型"
    if en_asr == "parakeet":
        required["parakeet-en-unified"] = "英语识别模型（缺了英语会话会退回 SenseVoice）"
    for name, label in required.items():
        entry = CATALOG.get(name)
        if entry is None:
            continue
        ready = is_ready(entry)
        print(f"      {label}（{name}）：{'已就绪' if ready else '缺失'}")
        if not ready:
            missing.append(name)

    if engine == "hymt":
        try:
            import llama_cpp  # noqa: F401
        except ImportError:
            print("      翻译推理库 llama-cpp-python：缺失")
            missing.append("llama-cpp-python")
    else:
        # 翻译模型的 CT2 版是转换产物，不在 catalog 的必需文件里，单独看
        ct2 = MODELS_DIR / "Qwen2.5-0.5B-Instruct-ct2-int8" / "model.bin"
        print(f"      翻译模型 CT2 版：{'已就绪' if ct2.is_file() else '缺失'}")
        if not ct2.is_file():
            missing.append("Qwen2.5-0.5B-Instruct-ct2-int8")

    for name in missing:
        if name == "hy-mt2":
            print("      → 缺它服务无法带翻译启动。下载（约 1.13GB）：")
            print("        python tools/setup_models.py --engine hy-mt2")
        elif name == "llama-cpp-python":
            print("      → 安装（预编译 CPU 版，不需要编译器）：")
            print("        pip install llama-cpp-python==0.3.35 "
                  "--extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu")
        elif name.lower().startswith("qwen"):
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
    parser.add_argument("--no-preload", action="store_true", help="不在启动后预加载英语识别模型")
    parser.add_argument("--language", default="ja")
    parser.add_argument("--en-asr", default="parakeet", choices=("parakeet", "sensevoice"),
                        help="英语会话的识别模型：parakeet（默认，Parakeet unified 英语版，更准）或 sensevoice（更快）")
    parser.add_argument("--asr", default="parakeet", choices=tuple(ASR_MODEL) + tuple(HYBRID),
                        help="识别模型：parakeet（默认，日语专用、更准）；sensevoice（多语言、更快，英语用它）；"
                             "hybrid（草稿 SenseVoice、定稿 Parakeet，实验）")
    parser.add_argument("--translate-engine", default="hymt", choices=("hymt", "instruct"),
                        help="hymt=Hy-MT2-1.8B（默认）；instruct=Qwen2.5-0.5B（备选）")
    parser.add_argument("--log", default=None, help="把会话事件写入指定 JSONL（覆盖默认的按会话日志）")
    parser.add_argument("--no-log", action="store_true",
                        help="不写会话日志（默认每次会话写一份到 runs/live/，只保留最近 20 份）")
    parser.add_argument("--threads", type=int, default=None, help="识别线程数（默认 CPU 逻辑核数 / 3）")
    parser.add_argument("--partial-step", type=float, default=None, help="覆盖所有语言的草稿间隔（英语 Parakeet 默认 0.75，其他 0.5 秒）")
    parser.add_argument("--en-threads", type=int, default=None, help="仅覆盖英语识别线程数（英语 Parakeet 默认最多 3）")
    parser.add_argument("--en-partial-step", type=float, default=None, help="仅覆盖英语草稿刷新间隔")
    parser.add_argument("--idle-exit", type=float, default=0, metavar="SECONDS",
                        help="连续这么多秒没有采集就自动退出（0 = 不退出，扩展一键启动时会传入）")
    parser.add_argument("--async-finals", action="store_true",
                        help="实验：定稿放进独立线程（09-30 实测更慢，默认关闭）")
    parser.add_argument("--early-final", action="store_true",
                        help="实验：两句被 VAD 粘在一起时前半句提前定稿（默认关闭）")
    parser.add_argument("--adaptive-silence", type=float, default=None, metavar="SECONDS",
                        help="实验：partial 以句末助词/问号结尾时，静音达到这么多秒就定稿（如 0.2，默认关闭）")
    args = parser.parse_args()

    print("=" * 60)
    print("  本地实时字幕 - 流式服务")
    print("=" * 60)
    print()

    check_models(args.translate_engine, args.asr, args.en_asr)
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

    final = None
    if args.asr in HYBRID:
        (asr_engine, asr_model), final = HYBRID[args.asr]
    else:
        asr_engine, asr_model = ASR_MODEL[args.asr]
    argv = ["app.server", "--engine", asr_engine, "--model", asr_model,
            "--host", args.host, "--port", str(args.port), "--language", args.language]
    if final:
        argv += ["--final-engine", final[0], "--final-model", final[1]]
    argv += (["--en-engine", "sherpa", "--en-model", "parakeet-en-unified"] if args.en_asr == "parakeet"
             else ["--en-engine", "sensevoice", "--en-model", "sensevoice-2024"])
    if not args.no_translate:
        argv += ["--translate", "--translate-engine", args.translate_engine]
    if args.no_preload:
        argv += ["--no-preload"]
    # 默认写日志：用户实测后能直接复盘逐句识别、延迟和过滤情况（09-30 双开关实测就因为没日志没法分析）。
    # 只存文本和时间，一小时约 1.6MB，会话结束时写一次。
    if args.log:
        argv += ["--log", args.log]
    elif not args.no_log:
        argv += ["--log-dir", str(ROOT / "runs" / "live")]
    if args.async_finals:
        argv += ["--async-finals"]
    if args.idle_exit:
        argv += ["--idle-exit", str(args.idle_exit)]
    if args.threads:
        argv += ["--threads", str(args.threads)]
    if args.partial_step:
        argv += ["--partial-step", str(args.partial_step)]
    if args.en_threads:
        argv += ["--en-threads", str(args.en_threads)]
    if args.en_partial_step:
        argv += ["--en-partial-step", str(args.en_partial_step)]
    if args.early_final:
        argv += ["--early-final"]
    if args.adaptive_silence is not None:
        argv += ["--adaptive-silence", str(args.adaptive_silence)]
    sys.argv = argv

    from app.server import main as server_main
    try:
        server_main()
    except KeyboardInterrupt:
        print("\n已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
