"""离线回放台架：把一段 wav 按实时速度喂给流水线，输出逐事件的延迟分解。

这是整个重构的关键工具（ARCHITECTURE_REVIEW.md M1）。
在此之前，验证一次流式延迟要开 Chrome、开 YouTube、肉眼看；有了它，
每次调参都是"跑一条命令、看 P95 变了多少"，架构实验的反馈周期从分钟降到秒。

为什么用虚拟时钟而不是真的 sleep：
音频按 1x 到达，计算耗时按真实测量值推进"模拟墙钟"。于是

    观众延迟 = finish_wall - audio_end

这个式子把 CPU 成本直接算进延迟里——异步、多线程、流水线都不能把它藏起来，
藏起来的部分会以"墙钟落后"的形式出现在后续事件上。这正是我们想量的东西。

用法：
    python tools/replay.py --wav sample_0230_0300.wav --pipeline fixed_chunk \\
        --model base --chunk 2.0 --min-speech 0 --out runs/baseline_base_2s.jsonl
"""

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from faster_whisper.audio import decode_audio  # noqa: E402

from app.asr.faster_whisper_engine import WhisperEngine  # noqa: E402
from app.pipelines.fixed_chunk import FixedChunkPipeline  # noqa: E402

SAMPLE_RATE = 16000

# 原始原型的识别参数，用来复现"停滞版本"的行为做对照。
# 与 app/asr/faster_whisper_engine.py 的 DEFAULT_ASR_KWARGS 的差异就是 M0 的参数修复。
LEGACY_ASR_KWARGS = {
    "temperature": [0.0, 0.2, 0.4, 0.6, 0.8, 1.0],
    "repetition_penalty": 1.0,
    "no_repeat_ngram_size": 0,
    "condition_on_previous_text": True,
}


DEFAULT_MODEL = {"whisper": "base", "sensevoice": "sensevoice-2024"}


def build_engine(args, model_name):
    """按 --engine 构造识别引擎。台架只依赖 transcribe/warm_up/name 三个接口。"""
    if args.engine == "sensevoice":
        if args.legacy_params:
            raise SystemExit("--legacy-params 只对 whisper 有意义")
        from app.asr.sensevoice_engine import SenseVoiceEngine
        return SenseVoiceEngine(model_name, num_threads=args.threads, language=args.language)
    overrides = LEGACY_ASR_KWARGS if args.legacy_params else {}
    if overrides:
        print(f"legacy ASR params: {overrides}", flush=True)
    return WhisperEngine(model_name, **overrides)


def build_pipeline(args, engine):
    if args.pipeline == "fixed_chunk":
        return FixedChunkPipeline(
            engine,
            chunk_seconds=args.chunk,
            language=args.language,
            min_speech=args.min_speech,
        )
    raise SystemExit(f"unknown pipeline: {args.pipeline}")


def replay(pipeline, pcm, step, verbose=False):
    """驱动流水线跑完一段音频，返回事件列表与统计。"""
    duration = len(pcm) / float(SAMPLE_RATE)
    cursor = 0
    wall = 0.0
    total_service = 0.0
    events = []

    while True:
        # 音频按 1x 到达：wall 之前的音频全部可用。
        # wall 可能因为计算耗时而越过音频总长（流水线跟不上），此时仍然要把
        # 剩下的音频补齐——否则"慢"会被错误地表现为"少处理了一段"，把结论算反。
        target = int(round(min(wall, duration) * SAMPLE_RATE))
        if target > cursor:
            pipeline.push_audio(pcm[cursor:target])
            cursor = target

        job = pipeline.next_job(available_until=wall, eof=cursor >= len(pcm))
        if job is not None:
            started = time.perf_counter()
            produced = pipeline.run_job(job)
            service = time.perf_counter() - started

            total_service += service
            wall += service

            for event in produced:
                event.service_seconds = service
                event.finish_wall = wall
                events.append(event)
                if verbose:
                    print(
                        f"  seg={event.segment_id:03d} audio={event.audio_start:6.2f}-{event.audio_end:6.2f} "
                        f"wall={wall:7.2f} service={service:5.2f} "
                        f"lat_mean={event.latency_mean:6.2f} lat_worst={event.latency_worst:6.2f} "
                        f"text={event.text[:50]}",
                        flush=True,
                    )
            continue

        # 没有作业可做：音频还没到齐就推进时间，到齐了就结束。
        if cursor >= len(pcm) and wall >= duration:
            break
        if wall < duration:
            wall = min(wall + step, duration)

    return events, duration, total_service, wall


def percentile(values, q):
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * q
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def summarize(args, pipeline, events, duration, total_service, final_wall):
    latencies = [event.latency_mean for event in events]
    worst = [event.latency_worst for event in events]
    return {
        "type": "summary",
        "pipeline": args.pipeline,
        "engine": args.engine,
        "model": args.model,
        "language": args.language,
        "chunk_seconds": args.chunk,
        "min_speech": args.min_speech,
        "legacy_params": args.legacy_params,
        "step": args.step,
        "audio_seconds": round(duration, 3),
        "events": len(events),
        "calls": pipeline.stats.get("calls"),
        "gated_skips": pipeline.stats.get("skipped"),
        "total_service_seconds": round(total_service, 3),
        "final_wall_seconds": round(final_wall, 3),
        # cpu_ratio < 1 才代表 CPU 跟得上；> 1 代表无论算法多好，墙钟必然单调落后。
        "cpu_ratio": round(total_service / duration, 3),
        "e2e_ratio": round(final_wall / duration, 3),
        "latency_p50": round(percentile(latencies, 0.50), 3),
        "latency_p95": round(percentile(latencies, 0.95), 3),
        "latency_max": round(max(latencies), 3) if latencies else 0.0,
        "latency_worst_max": round(max(worst), 3) if worst else 0.0,
    }


MEDIAN_KEYS = ("cpu_ratio", "e2e_ratio", "latency_p50", "latency_p95", "latency_worst_max")


def median_summary(summaries):
    """多次重复取中位数。

    单次回放的 cpu_ratio 实测会在 0.28 ~ 0.60 之间波动（同一份输入、同一套参数），
    所以调参必须看中位数，不能看单次结果。
    """
    result = dict(summaries[0])
    for key in MEDIAN_KEYS:
        result[key] = round(statistics.median([s[key] for s in summaries]), 3)
    result["repeats"] = [s["cpu_ratio"] for s in summaries]
    result["calls"] = summaries[0]["calls"]
    result["gated_skips"] = summaries[0]["gated_skips"]
    result["events"] = summaries[0]["events"]
    return result


def main():
    parser = argparse.ArgumentParser(description="虚拟时钟回放台架")
    parser.add_argument("--wav", type=Path, required=True)
    parser.add_argument("--pipeline", default="fixed_chunk")
    parser.add_argument("--engine", default="whisper", choices=("whisper", "sensevoice"))
    parser.add_argument("--model", default=None,
                        help="whisper 用 tiny/base/small；sensevoice 用模型目录名或路径")
    parser.add_argument("--threads", type=int, default=None, help="sensevoice 线程数")
    parser.add_argument("--language", default="ja")
    parser.add_argument("--chunk", type=float, default=2.0, help="固定分块流水线的块长度")
    parser.add_argument("--min-speech", type=float, default=0.0, help="VAD 语音时长门控阈值")
    parser.add_argument("--step", type=float, default=0.1, help="音频到达的仿真步长（秒）")
    parser.add_argument("--limit", type=float, default=None, help="只回放前 N 秒")
    parser.add_argument("--repeat", type=int, default=1, help="重复次数，取中位数（单次噪声很大）")
    parser.add_argument("--no-warmup", action="store_true")
    parser.add_argument("--legacy-params", action="store_true",
                        help="使用原型的识别参数（默认温度阶梯 + 不抑制复读），用于复现旧行为")
    parser.add_argument("--out", type=Path, default=None)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    pcm = decode_audio(str(args.wav), sampling_rate=SAMPLE_RATE)
    if args.limit:
        pcm = pcm[: int(args.limit * SAMPLE_RATE)]
    source_duration = len(pcm) / float(SAMPLE_RATE)

    model_name = args.model or DEFAULT_MODEL[args.engine]
    print(f"wav={args.wav.name} duration={source_duration:.2f}s engine={args.engine} "
          f"model={model_name} pipeline={args.pipeline} repeat={args.repeat}", flush=True)

    engine = build_engine(args, model_name)
    args.model = model_name
    if not args.no_warmup:
        began = time.perf_counter()
        engine.warm_up(args.language)
        print(f"warm-up={time.perf_counter() - began:.2f}s", flush=True)

    rounds = []
    for index in range(args.repeat):
        pipeline = build_pipeline(args, engine)
        events, duration, total_service, final_wall = replay(
            pipeline, pcm, args.step, verbose=not args.quiet and args.repeat == 1
        )
        round_summary = summarize(args, pipeline, events, duration, total_service, final_wall)
        rounds.append((round_summary, events, pipeline))
        if args.repeat > 1:
            print(f"  round {index + 1}/{args.repeat}: cpu_ratio={round_summary['cpu_ratio']} "
                  f"p50={round_summary['latency_p50']} p95={round_summary['latency_p95']}",
                  flush=True)

    summary = median_summary([r[0] for r in rounds]) if args.repeat > 1 else rounds[0][0]

    # 落盘用最接近中位数的那一轮，避免把一次异常轮次的逐事件明细当成代表。
    median_cpu = summary["cpu_ratio"]
    chosen = min(rounds, key=lambda item: abs(item[0]["cpu_ratio"] - median_cpu))
    events = chosen[1]

    print()
    print("---- summary ----")
    for key, value in summary.items():
        if key != "type":
            print(f"{key}={value}")

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps({
                "type": "header",
                "wav": str(args.wav),
                "repeat": args.repeat,
                **{k: v for k, v in summary.items() if k != "type"},
            }, ensure_ascii=False) + "\n")
            for event in events:
                handle.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
        print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
