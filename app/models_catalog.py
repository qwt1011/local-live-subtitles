"""模型目录表。

放在 app/ 而不是 tools/，因为下载脚本和识别引擎都要用它——
否则就会出现"安装脚本认一个名字、引擎认另一个名字"的错位。
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODELS_DIR = ROOT / "models"

GITHUB = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models"

CATALOG = {
    "sensevoice": {
        "archive": "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09.tar.bz2",
        "dir": "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09",
        "model": "model.int8.onnx",
        "tokens": "tokens.txt",
        "hf": "csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09",
        "languages": "zh, en, ja, ko, yue",
        "note": "非自回归 CTC 结构，没有 Whisper 那种'补齐到 30 秒'的固定成本",
    },
    "sensevoice-2024": {
        "archive": "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17.tar.bz2",
        "dir": "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2024-07-17",
        "model": "model.int8.onnx",
        "tokens": "tokens.txt",
        "hf": "csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17",
        "languages": "zh, en, ja, ko, yue",
        "note": "上一版，自带 ja.wav 测试样本，作为回退选项",
    },
}


def is_ready(entry):
    directory = MODELS_DIR / entry["dir"]
    return (directory / entry["model"]).is_file() and (directory / entry["tokens"]).is_file()


def resolve(name_or_path):
    """把目录名、目录路径或 catalog 键解析成实际模型目录。"""
    path = Path(name_or_path)
    if path.is_dir():
        return path
    candidate = MODELS_DIR / name_or_path
    if candidate.is_dir():
        return candidate
    entry = CATALOG.get(name_or_path)
    if entry is not None:
        directory = MODELS_DIR / entry["dir"]
        if directory.is_dir():
            return directory
    raise SystemExit(
        f"找不到模型：{name_or_path}\n"
        f"已知条目：{', '.join(sorted(CATALOG))}\n"
        f"先运行：python tools/setup_models.py --engine {name_or_path}"
    )
