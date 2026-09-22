"""回放结果分析：延迟分解 + 文本质量。

用法：
    python tools/metrics.py runs/baseline.jsonl
    python tools/metrics.py runs/baseline.jsonl runs/streaming.jsonl --reference reference_small.txt
    python tools/metrics.py runs/baseline.jsonl --show

核心指标只有两个：

  cpu_ratio  = 总计算耗时 / 音频时长
               < 1 才代表这套架构在纯 CPU 上**跑得动**。> 1 时无论算法多好，
               墙钟都会单调落后，字幕延迟无上界——这正是"停滞版本"的病根。

  p50/p95/worst = 音节第一次出现在屏幕上的延迟（口径见 app/events.py::latency_views）
  first_p95     = 句子开口到屏幕上有字
  final_p95     = 说完最后一个字到文本不再变化（定稿延迟）

  只报最后一个事件的延迟会把流式增量架构算得比实际差很多（M2 实测：错口径 p95 3.26 秒，
  正确口径 0.66 秒），所以延迟一律由 app/events.py::latency_views 统一计算，台架与报告共用。
"""

import argparse
import json
import statistics
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.events import latency_views, percentile  # noqa: E402

# 计算字错率时去掉的字符：空白与标点。
# 注意不要去掉「ー」（长音符）和「っ」（促音），它们在日语里是实义字符。
PUNCTUATION = set(" \t\r\n\u3000。、，．,.!?！？：:；;「」『』（）()［］[]{}…‥·〜~-—_\"'“”‘’")


def load(path):
    header = {}
    events = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get("type") == "header":
                header = row
            else:
                events.append(row)
    return header, events


def normalize(text):
    out = []
    for char in unicodedata.normalize("NFKC", text):
        if char in PUNCTUATION:
            continue
        out.append(char)
    return "".join(out)


def levenshtein(source, target):
    if not source:
        return len(target)
    if not target:
        return len(source)
    previous = list(range(len(target) + 1))
    for i, s_char in enumerate(source, start=1):
        current = [i]
        for j, t_char in enumerate(target, start=1):
            current.append(min(
                previous[j] + 1,            # 删除
                current[j - 1] + 1,         # 插入
                previous[j - 1] + (s_char != t_char),  # 替换
            ))
        previous = current
    return previous[-1]


def character_error_rate(hypothesis, reference):
    ref = normalize(reference)
    hyp = normalize(hypothesis)
    if not ref:
        return 0.0 if not hyp else 1.0
    return levenshtein(ref, hyp) / len(ref)


def final_transcript(events):
    """每个 segment 取 revision 最大的一条，按 segment_id 排序拼起来。"""
    latest = {}
    for event in events:
        key = event.get("segment_id", 0)
        if key not in latest or event.get("revision", 0) >= latest[key].get("revision", 0):
            latest[key] = event
    ordered = [latest[key] for key in sorted(latest)]
    return " ".join(row.get("text", "").strip() for row in ordered if row.get("text", "").strip())


def analyze(path, reference=None):
    header, events = load(path)
    views = latency_views(events)
    result = {
        "run": path.name,
        "pipeline": header.get("pipeline", "?"),
        "engine": header.get("engine", "whisper"),
        "model": header.get("model", "?"),
        "chunk": header.get("chunk_seconds"),
        "min_speech": header.get("min_speech"),
        "calls": header.get("calls"),
        "gated": header.get("gated_skips"),
        "segments": len({row.get("segment_id") for row in events}),
        "cpu_ratio": header.get("cpu_ratio"),
        "e2e_ratio": header.get("e2e_ratio"),
        # 头条指标：音节第一次出现在屏幕上的平均延迟
        "p50": percentile(views["mean"], 0.50),
        "p95": percentile(views["mean"], 0.95),
        "worst": max(views["worst"]) if views["worst"] else 0.0,
        # 定稿延迟：说完最后一个字到文本不再变化
        "final_p95": percentile(views["final_stale"], 0.95),
        "first_p95": percentile(views["first_show"], 0.95),
        "svc_mean": statistics.fmean([row["service_seconds"] for row in events]) if events else 0.0,
        "transcript": final_transcript(events),
    }
    if reference is not None:
        result["cer"] = character_error_rate(result["transcript"], reference)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("runs", type=Path, nargs="+")
    parser.add_argument("--reference", type=Path, default=None,
                        help="伪参考文本（见 tools/offline_reference.py）")
    parser.add_argument("--show", action="store_true", help="打印逐事件明细")
    args = parser.parse_args()

    reference = args.reference.read_text(encoding="utf-8").strip() if args.reference else None

    reports = [analyze(path, reference) for path in args.runs]

    columns = ["run", "pipeline", "engine", "model", "chunk", "min_speech", "calls", "gated",
               "segments", "cpu_ratio", "first_p95", "p50", "p95", "worst", "final_p95", "cer"]
    widths = {"run": 26, "pipeline": 15, "engine": 10, "model": 14}
    print(" | ".join(column.ljust(widths.get(column, len(column))) for column in columns))
    for report in reports:
        cells = []
        for column in columns:
            value = report.get(column)
            if value is None:
                text = "-"
            elif isinstance(value, float):
                text = f"{value:.3f}"
            else:
                text = str(value)
            cells.append(text.ljust(widths.get(column, len(column))))
        print(" | ".join(cells))

    print()
    print("单位秒。first_p95=句子开口到屏幕有字；p50/p95/worst=音节首次出现的延迟；"
          "final_p95=说完到文本不再变化。口径说明见 latency_views()。")

    for report in reports:
        print()
        print(f"---- {report['run']} transcript ----")
        print(report["transcript"])

    if args.show:
        for path in args.runs:
            _, events = load(path)
            print()
            print(f"---- {path.name} events ----")
            for row in events:
                mid = (row["audio_start"] + row["audio_end"]) / 2.0
                print(f"seg={row['segment_id']:03d} rev={row['revision']} "
                      f"audio={row['audio_start']:6.2f}-{row['audio_end']:6.2f} "
                      f"wall={row['finish_wall']:7.2f} svc={row['service_seconds']:5.2f} "
                      f"lat_mean={row['finish_wall'] - mid:6.2f} "
                      f"lat_worst={row['finish_wall'] - row['audio_start']:6.2f} "
                      f"final={int(row['is_final'])} {row.get('text', '')}")

    if reference is not None:
        print()
        print("参考文本为伪参考（大模型整段输出），只能做相对比较，不是绝对准确率。")


if __name__ == "__main__":
    main()
