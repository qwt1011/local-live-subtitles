"""Measure bounded postprocessing; real-session summaries never export transcripts."""

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.translate.address_memory import AddressMemory


CASES = [
    ("ja", "お兄さん、こっち。", "小哥，过来。", "哥哥，过来。"),
    ("ja", "ねえ、お兄さん。", "嘿，小哥。", "嘿，哥哥。"),
    ("en", "Come here, dork.", "过来，呆子。", "过来，笨蛋。"),
    ("en", "Listen, dummy.", "听着，傻瓜。", "听着，笨蛋。"),
    ("ja", "彼のお兄さん。", "他的小哥。", "他的小哥。"),
    ("ja", "君のお兄さん。", "你的小哥。", "你的小哥。"),
    ("ja", "お兄さんは医者です。", "小哥是医生。", "小哥是医生。"),
    ("ja", "お兄さん。", "哥哥还是小哥？", "哥哥还是小哥？"),
    ("ja", "お兄さん。", "小哥，小哥！", "小哥，小哥！"),
    ("ja", "お兄さん。", "他说小哥来了。", "他说小哥来了。"),
    ("en", "He is a dummy.", "他是个呆子。", "他是个呆子。"),
    ("en", "I bought a dummy.", "我买了一个假人。", "我买了一个假人。"),
    ("en", "You better pick up Dork", "你最好去接Dork", "你最好去接Dork"),
    ("en", "Good boy.", "真乖。", "真乖。"),
]


def seed():
    memory = AddressMemory()
    for language, text, translated in (("ja", "かわいいお兄さん。", "可爱的哥哥。"),
                                       ("en", "Whatever, Dork. Listen.", "随便吧，笨蛋。听我说。"),
                                       ("en", "Hey, dummy.", "嘿，笨蛋。")):
        memory.apply(text, translated, language)
    return memory


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sessions", type=Path)
    parser.add_argument("--out", type=Path, default=ROOT / "runs/address_memory/postprocess.json")
    args = parser.parse_args()
    durations, results = [], []
    for language, text, translated, expected in CASES:
        memory = seed()
        for _ in range(1000):
            began = time.perf_counter_ns()
            output, change = memory.apply(text, translated, language)
            durations.append((time.perf_counter_ns() - began) / 1e6)
        assert output == expected, (text, output, expected)
        results.append(dict(source="synthetic boundary test", text=text, original=translated,
                            output=output, changed=change is not None))
    report = dict(cases=results, calls=len(durations), model_calls=0,
                  overhead_ms_p50=statistics.median(durations),
                  overhead_ms_p95=sorted(durations)[int(len(durations) * .95)])
    if args.sessions:
        files, translated, changes = 0, 0, 0
        for path in args.sessions.glob("*.jsonl"):
            memory = AddressMemory()
            files += 1
            for line in path.read_text(encoding="utf-8").splitlines():
                event = json.loads(line)
                if not event.get("translation"):
                    continue
                text = event["text"]
                language = "ja" if any("\u3040" <= c <= "\u30ff" for c in text) else "en"
                _, change = memory.apply(text, event["translation"], language)
                translated += 1
                changes += change is not None
        report["private_session_summary"] = dict(files=files, translations=translated, changes=changes,
                                                 note="Counts only; transcripts and paths excluded")
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k != "cases"}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
