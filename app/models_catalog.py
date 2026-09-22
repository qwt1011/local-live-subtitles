"""模型目录表。

放在 app/ 而不是 tools/，因为下载脚本和识别引擎都要用它——
否则就会出现"安装脚本认一个名字、引擎认另一个名字"的错位。
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODELS_DIR = ROOT / "models"

GITHUB = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models"

# 下载源与实测速度（见 tools/setup_models.py 的 docstring）。
HF_MIRRORS = [
    "https://hf-mirror.com/{repo}/resolve/main/{file}",
    "https://huggingface.co/{repo}/resolve/main/{file}",
]

# 翻译模型。
#
# 注意：**`Helsinki-NLP/opus-mt-ja-zh` 并不存在**（只有 zh->ja 的反向模型），
# 也没有 `opus-mt-mul-zh`。所以"改用 opus-mt 直连 ja->zh"这个方案在模型层面不成立，
# 直连 ja->zh 的现实选择是 NLLB（这也正是最初的选择）。
# 这里用的是**已经转换好的 CTranslate2 int8** 版本，因此不需要安装 transformers、
# 也不需要在本机跑一遍转换（省掉约 2.4GB 的 fp32 下载）。
NLLB_LANGS = {"ja": "jpn_Jpan", "zh": "zho_Hans", "en": "eng_Latn"}

CATALOG = {
    "nllb-ja-zh-hf": {
        "kind": "hf",
        "repo": "facebook/nllb-200-distilled-600M",
        "dir": "nllb-200-distilled-600M-hf",
        "files": [
            "sentencepiece.bpe.model",
            "config.json",
            "generation_config.json",
            "special_tokens_map.json",
            "tokenizer.json",
            "tokenizer_config.json",
            "pytorch_model.bin",
        ],
        "required": ["pytorch_model.bin", "sentencepiece.bpe.model"],
        "languages": "ja -> zh（jpn_Jpan -> zho_Hans）",
        "note": "NLLB 原始 fp32 权重（约 2.35GB），用于**本地**转成 CT2。"
                "之所以要自己转：两份现成的 CT2 int8 仓库都实测坏掉了"
                "（shared_vocabulary 与 model.bin 的 embedding 行错位），"
                "而本地转换能产出词表一致的模型——已用 Marian 验证过这条路。",
    },
    "nllb-ja-zh": {
        "kind": "hf",
        "repo": "JustFrederik/nllb-200-distilled-600M-ct2-int8",
        "dir": "nllb-200-distilled-600M-ct2-int8",
        "files": [
            "model.bin",
            "sentencepiece.bpe.model",
            "shared_vocabulary.txt",
            "tokenizer.json",
            "config.json",
            "special_tokens_map.json",
            "tokenizer_config.json",
        ],
        "required": ["model.bin", "sentencepiece.bpe.model"],
        "languages": "ja -> zh（NLLB 用 jpn_Jpan -> zho_Hans），也支持 en -> zh",
        "note": "NLLB-200-distilled-600M 的 CT2 int8 版本，约 600MB，直连不需要中转",
        "status": "已实测不可用：解码退化成复读源词（详见 BENCHMARK_RESULTS.md 第 12 节）",
    },
    "nllb-ja-zh-alt": {
        "kind": "hf",
        "repo": "osa911/nllb-200-distilled-600M-ct2-int8",
        "dir": "nllb-200-distilled-600M-ct2-int8-osa911",
        "files": [
            "model.bin",
            "sentencepiece.bpe.model",
            "shared_vocabulary.json",
            "manifest.json",
            "config.json",
        ],
        "required": ["model.bin", "sentencepiece.bpe.model"],
        "languages": "ja -> zh（NLLB 用 jpn_Jpan -> zho_Hans），也支持 en -> zh",
        "note": "另一份独立转换（model.bin 591.0MB，带 shared_vocabulary.json + manifest.json），"
                "用于判定上一份是不是转换本身坏了",
    },
    "sensevoice": {
        "kind": "tar",
        "archive": "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09.tar.bz2",
        "dir": "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09",
        "model": "model.int8.onnx",
        "tokens": "tokens.txt",
        "hf": "csukuangfj/sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09",
        "languages": "zh, en, ja, ko, yue",
        "note": "非自回归 CTC 结构，没有 Whisper 那种'补齐到 30 秒'的固定成本",
    },
    "sensevoice-2024": {
        "kind": "tar",
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
    if entry.get("kind") == "hf":
        return all((directory / name).is_file() for name in entry["required"])
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
