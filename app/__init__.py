"""ASMR 实时字幕的流式内核。

分层（详见 docs/ARCHITECTURE_REVIEW.md 第 4 节）：

    audio/      环形缓冲、VAD 门控、分段
    asr/        识别引擎（协议 + faster-whisper 实现；后续可加 SenseVoice）
    translate/  翻译引擎（M5）
    pipelines/  "台架可驱动的流水线"实现：固定分块基线 + 流式内核

时间轴约定贯穿全项目：
- 所有 `audio_*` 字段是音频时间轴（秒），从捕获开始算起。
- `finish_wall` 是模拟墙钟（秒），同样从捕获开始算起，音频按 1x 到达。
- 延迟有三个口径，都必须报，只报一个会误导：
    latency_end   = finish_wall - audio_end    该段最后一个音节（乐观）
    latency_of_mean = finish_wall - 段中点      段内平均延迟（头条指标）
    latency_worst = finish_wall - audio_start  该段第一个音节（最坏）
  固定分块下三者相差一个块长度；流式增量的设计里三者应该很接近。
  CPU 成本被直接算进延迟，异步与多线程都无法把它藏起来——藏起来的部分
  会以"墙钟落后"的形式出现在后续事件上。
"""
