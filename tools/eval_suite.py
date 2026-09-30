"""在整个评测集上跑回放台架，输出逐片段与汇总的延迟 / 字错率。

    python tools/eval_suite.py --model sensevoice-2024 --tag sv2024
    python tools/eval_suite.py --model sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09 --tag sv2025

结果写到 runs/eval/<tag>/<clip>.jsonl（与 replay.py 同格式）和 runs/eval/<tag>/summary.json。
字错率分两列：cer 只算 status 为 consensus / verified 的片段（正式结论只看它）；
cer_draft 算全部片段，含未审校的草稿。参考文本的分级见 eval/README.md。
"""

import argparse
import json
import statistics
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from faster_whisper.audio import decode_audio  # noqa: E402

from tools.metrics import character_error_rate, final_transcript, load_reference, reference_status  # noqa: E402
from tools.replay import build_engine, build_pipeline, replay, summarize  # noqa: E402

SAMPLE_RATE = 16000
EVAL_DIR = ROOT / "eval"
SCORED = ("consensus", "verified")


def run_clip(args, engine, clip, pcm, final_engine=None):
    options = SimpleNamespace(
        pipeline="open_utterance", engine=args.engine, model=args.model, language=clip["language"],
        chunk=None, min_speech=args.min_speech, min_silence=args.min_silence,
        partial_step=args.partial_step, max_utterance=args.max_utterance,
        call_timeout=None, legacy_params=False, step=0.1, keep_fillers=args.keep_fillers,
        early_final=args.early_final, adaptive_silence=args.adaptive_silence,
        final_engine_obj=final_engine,
    )
    rounds = []
    for _ in range(args.repeat):
        pipeline = build_pipeline(options, engine)
        events, duration, total_service, final_wall = replay(pipeline, pcm, options.step)
        rounds.append((summarize(options, pipeline, events, duration, total_service, final_wall), events))
    median_cpu = statistics.median(r[0]["cpu_ratio"] for r in rounds)
    summary, events = min(rounds, key=lambda r: abs(r[0]["cpu_ratio"] - median_cpu))
    return summary, [event.to_dict() for event in events]


def main():
    parser = argparse.ArgumentParser(description="评测集批量回放")
    parser.add_argument("--engine", default="sensevoice", choices=("whisper", "sensevoice", "sherpa"))
    parser.add_argument("--model", default="sensevoice-2024")
    parser.add_argument("--tag", required=True, help="结果目录名，例如 sv2024")
    parser.add_argument("--clips", nargs="*", default=None, help="只跑这些片段 id")
    parser.add_argument("--repeat", type=int, default=3, help="每段重复次数，取 cpu_ratio 中位数那轮")
    # 默认值与 app/server.py 保持一致，评测的就是线上配置。
    parser.add_argument("--min-speech", type=float, default=0.4)
    parser.add_argument("--min-silence", type=float, default=0.35)
    parser.add_argument("--partial-step", type=float, default=0.5)
    parser.add_argument("--max-utterance", type=float, default=10.0)
    parser.add_argument("--keep-fillers", action="store_true", help="关闭语气词过滤（对照用）")
    parser.add_argument("--early-final", action="store_true", help="开启句中提前定稿（实验）")
    parser.add_argument("--adaptive-silence", type=float, default=None,
                        help="句末形式时的短静音阈值（秒，实验）")
    parser.add_argument("--final-engine", default=None, choices=("whisper", "sensevoice", "sherpa"),
                        help="定稿改用另一个引擎（草稿仍用 --engine），例如 sherpa")
    parser.add_argument("--final-model", default=None, help="--final-engine 的模型，例如 parakeet-ja")
    parser.add_argument("--threads", type=int, default=None, help="--engine 的线程数")
    parser.add_argument("--final-threads", type=int, default=None, help="--final-engine 的线程数")
    args = parser.parse_args()

    manifest = json.loads((EVAL_DIR / "manifest.json").read_text(encoding="utf-8"))
    clips = [c for c in manifest["clips"] if not args.clips or c["id"] in args.clips]
    out_dir = ROOT / "runs" / "eval" / args.tag
    out_dir.mkdir(parents=True, exist_ok=True)

    engines = {}
    final_engines = {}
    rows = []
    for clip in clips:
        wav = EVAL_DIR / "audio" / f"{clip['id']}.wav"
        if not wav.is_file():
            raise SystemExit(f"缺少 {wav}，先运行 python tools/build_eval.py")
        language = clip["language"]
        if language not in engines:
            engine_args = SimpleNamespace(engine=args.engine, language=language, threads=args.threads,
                                          legacy_params=False)
            engines[language] = build_engine(engine_args, args.model)
            engines[language].warm_up(language)
            if args.final_engine:
                final_args = SimpleNamespace(engine=args.final_engine, language=language,
                                             threads=args.final_threads, legacy_params=False)
                final_engines[language] = build_engine(final_args, args.final_model)
                final_engines[language].warm_up(language)
        pcm = decode_audio(str(wav), sampling_rate=SAMPLE_RATE)

        summary, events = run_clip(args, engines[language], clip, pcm, final_engines.get(language))
        transcript = final_transcript(events)

        ref_path = EVAL_DIR / "refs" / f"{clip['id']}.txt"
        status, cer = "missing", None
        if ref_path.is_file():
            raw = ref_path.read_text(encoding="utf-8")
            status = reference_status(raw)
            cer = character_error_rate(transcript, load_reference(raw))

        with (out_dir / f"{clip['id']}.jsonl").open("w", encoding="utf-8") as handle:
            handle.write(json.dumps({"type": "header", "wav": str(wav.relative_to(ROOT)),
                                     "clip": clip["id"], "ref_status": status,
                                     **{k: v for k, v in summary.items() if k != "type"}},
                                    ensure_ascii=False) + "\n")
            for event in events:
                handle.write(json.dumps(event, ensure_ascii=False) + "\n")

        row = {"clip": clip["id"], "tags": clip.get("tags", []), "ref": status,
               "cpu_ratio": summary["cpu_ratio"], "p50": summary["latency_p50"],
               "p95": summary["latency_p95"], "final_p95": summary["final_p95"],
               "worst": summary["latency_worst_max"], "cer": cer, "transcript": transcript}
        rows.append(row)
        print(f"{clip['id']}: cpu={row['cpu_ratio']:.3f} p95={row['p95']:.3f} "
              f"final_p95={row['final_p95']:.3f} cer={'-' if cer is None else f'{cer:.3f}'} ({status})",
              flush=True)

    def mean(key, subset):
        values = [r[key] for r in subset if r[key] is not None]
        return round(statistics.fmean(values), 4) if values else None

    verified = [r for r in rows if r["ref"] in SCORED]
    total = {
        "engine": args.engine, "model": args.model,
        "final_engine": args.final_engine, "final_model": args.final_model, "clips": len(rows),
        "scored_clips": len(verified),
        "cpu_ratio_mean": mean("cpu_ratio", rows),
        "p95_max": max(r["p95"] for r in rows),
        "final_p95_max": max(r["final_p95"] for r in rows),
        "cer": mean("cer", verified),
        "cer_draft": mean("cer", rows),
    }
    (out_dir / "summary.json").write_text(
        json.dumps({"total": total, "clips": rows}, ensure_ascii=False, indent=2), encoding="utf-8")

    print()
    print(f"{'clip':14} {'ref':9} {'cpu':>6} {'p50':>6} {'p95':>6} {'final95':>7} {'worst':>6} {'cer':>6}")
    for r in rows:
        cer = "-" if r["cer"] is None else f"{r['cer']:.3f}"
        print(f"{r['clip']:14} {r['ref']:9} {r['cpu_ratio']:6.3f} {r['p50']:6.3f} {r['p95']:6.3f} "
              f"{r['final_p95']:7.3f} {r['worst']:6.3f} {cer:>6}")
    print()
    print("汇总：" + ", ".join(f"{k}={v}" for k, v in total.items()))
    if not verified:
        print("注意：还没有审校过的参考文本，cer 为空；cer_draft 只是与草稿的一致度。")
    print(f"结果：{out_dir.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
