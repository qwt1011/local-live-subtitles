# legacy/：早期原型脚本

M0–M1 阶段（09-22 前后）的 Whisper 原型和一次性诊断脚本，已被 `app/`（服务与流水线）和 `tools/`（台架、评测）取代。
保留在这里，是因为 `BENCHMARK_RESULTS.md` 和 `ARCHITECTURE_REVIEW.md` 里的早期数字出自它们。

| 文件 | 当时的用途 | 现在用什么 |
|---|---|---|
| `local_service.py` | 最早的 Whisper HTTP 服务 | `app/server.py`（`启动字幕服务.bat`） |
| `benchmark.py` / `stream_benchmark.py` / `overlap_benchmark.py` | 固定分块、流式、重叠分块的速度对比 | `tools/replay.py`、`tools/eval_suite.py` |
| `diag_call_cost.py` | 每次 transcribe 调用的固定成本曲线 | — |
| `diag_fallback.py` | Whisper 温度回退与 VAD 门控的对照 | — |
| `install_argos_languages.py` | Argos 翻译语言包（已放弃这条路线） | `tools/setup_models.py` |

运行时在项目根目录执行，例如 `python legacy/diag_call_cost.py`。
