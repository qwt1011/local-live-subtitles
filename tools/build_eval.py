"""按 eval/manifest.json 切出评测片段，并为还没有参考文本的片段生成校对草稿。

音频不入库（源视频有版权）：先用 yt-dlp 把 manifest 里的源视频下载到项目根目录，再运行本脚本。
除 eval/audio/ 下的评测片段外，还会切出早期台架和 tests/test_ws_protocol.js 用的
sample_0230_0300.wav（manifest 的 "samples" 段）。

    python tools/build_eval.py            # 切片 + 生成缺失的草稿
    python tools/build_eval.py --force    # 重新生成全部草稿（只覆盖 draft；consensus / verified 永不覆盖）

草稿 = whisper small 整段上下文输出（每句一行，带片段内时间）
     + 以注释形式附上 SenseVoice 按 VAD 分句的输出。两者不一致的地方就是最值得人工听的地方。
参考文本的格式与解析见 tools/metrics.py::load_reference。
"""

import argparse
import json
import sys
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from faster_whisper.audio import decode_audio  # noqa: E402

from app.audio.vad import speech_regions  # noqa: E402
from tools.metrics import reference_status  # noqa: E402

SAMPLE_RATE = 16000
EVAL_DIR = ROOT / "eval"


def stamp(seconds):
    return f"[{int(seconds // 60):02d}:{seconds % 60:04.1f}]"


def write_wav(path, pcm):
    data = (np.clip(pcm, -1.0, 1.0) * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(data.tobytes())


def cut_clips(manifest, force):
    audio_dir = EVAL_DIR / "audio"
    audio_dir.mkdir(parents=True, exist_ok=True)
    decoded = {}
    clips = {}
    targets = [(audio_dir / f"{clip['id']}.wav", clip) for clip in manifest["clips"]]
    targets += [(ROOT / sample["path"], sample) for sample in manifest.get("samples", [])]
    for path, clip in targets:
        key = clip.get("id", path.stem)
        if path.is_file() and not force:
            clips[key] = decode_audio(str(path), sampling_rate=SAMPLE_RATE)
            continue
        source = manifest["sources"][clip["source"]]
        if clip["source"] not in decoded:
            source_path = ROOT / source["path"]
            if not source_path.is_file():
                raise SystemExit(f"找不到源文件：{source_path}\n可用 yt-dlp 从 {source.get('url')} 下载")
            print(f"解码 {source_path.name} ...", flush=True)
            decoded[clip["source"]] = decode_audio(str(source_path), sampling_rate=SAMPLE_RATE)
        full = decoded[clip["source"]]
        begin = int(clip["start"] * SAMPLE_RATE)
        pcm = full[begin: begin + int(clip["duration"] * SAMPLE_RATE)]
        write_wav(path, pcm)
        clips[key] = pcm
        print(f"  写出 {path.relative_to(ROOT)}（{len(pcm) / SAMPLE_RATE:.1f}s）", flush=True)
    return clips


def draft_text(clip, pcm, whisper, sensevoice):
    lines = [
        f"# id: {clip['id']}",
        "# status: draft",
        "# 审校流程见 eval/README.md；只有 status 为 consensus / verified 的片段计入正式字错率。",
        "# 规则：以 # 开头的行是注释；行首 [mm:ss.s] 时间戳可留可删，不计入文本。",
        "# 只写听到的话，不写拟声/呼吸描述；听不清的词写最可能的，并在行尾加 # ? 注释提醒自己。",
        f"# 片段：{clip['source']} {clip['start']}s 起，{clip['duration']}s，语言 {clip['language']}",
        "#",
        "# ---- SenseVoice 按 VAD 分句（仅供对照，不计入参考）----",
    ]
    for start, end in speech_regions(pcm):
        piece = pcm[int(start * SAMPLE_RATE): int(end * SAMPLE_RATE)]
        text = sensevoice.transcribe(piece, language=clip["language"]).text
        if text:
            lines.append(f"# {stamp(start)} {text}")
    lines.append("# ---- 以下为参考文本（草稿来源：whisper small 整段上下文）----")
    result = whisper.transcribe(pcm, language=clip["language"], condition_on_previous_text=True)
    for row in result.segments:
        if row["text"]:
            lines.append(f"{stamp(row['start'])} {row['text']}")
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description="生成评测片段与参考草稿")
    parser.add_argument("--force", action="store_true", help="重新切片并覆盖未校对的草稿")
    parser.add_argument("--whisper-model", default="small")
    parser.add_argument("--sensevoice-model", default="sensevoice-2024")
    args = parser.parse_args()

    manifest = json.loads((EVAL_DIR / "manifest.json").read_text(encoding="utf-8"))
    clips = cut_clips(manifest, args.force)

    refs = EVAL_DIR / "refs"
    refs.mkdir(parents=True, exist_ok=True)
    pending = []
    for clip in manifest["clips"]:
        path = refs / f"{clip['id']}.txt"
        if path.is_file():
            status = reference_status(path.read_text(encoding="utf-8"))
            if status != "draft" or not args.force:
                print(f"  跳过 {path.name}（{status}）", flush=True)
                continue
        pending.append(clip)
    if not pending:
        print("所有参考文本都已存在。")
        return

    from app.asr.factory import create_engine
    from app.asr.faster_whisper_engine import WhisperEngine

    print(f"加载 whisper {args.whisper_model} 与 {args.sensevoice_model} ...", flush=True)
    whisper = WhisperEngine(args.whisper_model)
    engines = {}
    for clip in pending:
        language = clip["language"]
        if language not in engines:
            engines[language] = create_engine("sensevoice", args.sensevoice_model, language=language)
        path = refs / f"{clip['id']}.txt"
        path.write_text(draft_text(clip, clips[clip["id"]], whisper, engines[language]),
                        encoding="utf-8")
        print(f"  写出草稿 {path.relative_to(ROOT)}", flush=True)


if __name__ == "__main__":
    main()
