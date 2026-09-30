"""本地服务冒烟测试：直接对 HTTP 接口打几段音频，检查返回结构与门控行为。

用法（先启动 legacy/local_service.py，这是早期 HTTP 原型的冒烟测试）：
    python tools/smoke_service.py --wav sample_0230_0300.wav --port 8765
"""

import argparse
import io
import json
import sys
import urllib.request
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def post(base, pcm_frames, params, label):
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as target:
        target.setparams(params)
        target.writeframes(pcm_frames)
    request = urllib.request.Request(
        f"{base}/transcribe?language=ja",
        data=buffer.getvalue(),
        headers={"Content-Type": "audio/wav"},
    )
    with urllib.request.urlopen(request, timeout=300) as response:
        data = json.load(response)
    text = " ".join(segment["text"] for segment in data.get("segments", []))
    print(
        f"[{label}] skipped={data.get('skipped')} "
        f"decode={data.get('decode_seconds')} vad={data.get('vad_seconds')} "
        f"asr={data.get('asr_seconds')} translate={data.get('translate_seconds')} "
        f"speech={data.get('speech_seconds')}/{data.get('audio_seconds')} "
        f"total={data.get('processing_seconds')} text={text[:70]}"
    )
    return data


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--wav", type=Path, required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    base = f"http://{args.host}:{args.port}"

    with urllib.request.urlopen(f"{base}/health", timeout=30) as response:
        print("health:", json.load(response))

    with wave.open(str(args.wav), "rb") as source:
        params = source.getparams()
        raw = source.readframes(source.getnframes())

    width = params.sampwidth * params.nchannels
    second = params.framerate * width

    post(base, raw[0 * second:2 * second], params, "2s 含语音")
    post(base, b"\x00" * second, params, "1s 纯静音（应被门控跳过）")
    post(base, raw[2 * second:4 * second], params, "2s 含语音 #2")
    post(base, raw[4 * second:10 * second], params, "6s 含语音")
    print("冒烟测试完成")


if __name__ == "__main__":
    main()
