# Work Log

## Current status

The project has a working offline transcription prototype, a localhost service, and a Chrome/Edge extension scaffold. Local Chinese translation is partially installed but Japanese translation is blocked by an unreliable Argos package download.

## Completed

- Confirmed target platform: Windows 11, Chrome/Edge, YouTube only for MVP.
- Confirmed subtitle modes: original, Chinese, and bilingual.
- Confirmed local-first and free operation as product constraints.
- Extracted `02:30-03:00` from the reference ASMR video to `sample_0230_0300.wav`.
- Benchmarked `faster-whisper base`: 30 seconds transcribed in 1.21 seconds.
- Benchmarked `faster-whisper small`: 30 seconds transcribed in 9.54 seconds with better Japanese accuracy.
- Tested naive 3-second chunks and overlapping windows; both were unstable for strict realtime ASMR.
- Implemented `local_service.py`, listening only on `127.0.0.1:8765`.
- Implemented `/health` and `/transcribe` endpoints with a persistent Whisper model.
- Added optional Argos translation and Japanese routing through `ja -> en -> zh`.
- Implemented Manifest V3 extension scaffold, YouTube subtitle overlay, service health check, tab audio capture, offscreen recording, and audio playback passthrough.
- Installed Argos Translate engine.
- Installed Argos `en -> zh` language package.

## In progress

- Argos `ja -> en` package download. The package is roughly 100 MB and repeated network interruptions prevent completion; a run exceeded 3000 seconds and was stopped.
- End-to-end browser test after translation packages are available.

## Latest browser test fixes

- Fixed CORS preflight handling for extension requests (`OPTIONS`).
- Confirmed browser audio requests reach the local service.
- Fixed Windows temporary WebM file locking before Whisper reads the file.
- Changed extension recording to send complete 3-second WebM recordings instead of non-decodable incremental blobs.
- Removed duplicate recorder callbacks in the offscreen document.
- Added service logging of recognized segment text and extension-side response logging.
- Current server logs show successful recognition responses; final verification of message delivery to the YouTube overlay is still pending.
- Subtitle overlay is now working in the browser; original/Chinese/bilingual display modes are being added.
- User feedback: perceived delay is at least one second; subtitle should be draggable and resizable; bilingual text should be stacked vertically; Japanese Chinese translation remains unavailable because `ja -> en` is not installed; English output intermittently does not appear despite successful server recognition.
- Implemented pointer dragging and mouse-wheel font resizing on the overlay.
- User reports English subtitles are now generally stable; main remaining issue is noticeable latency.
- Reduced extension recording interval from 3 seconds to 2 seconds and added `tiny` as a low-latency service model option.

## Known issues

- Argos has no direct `ja -> zh` package; Japanese translation uses English as an intermediate language and may lose nuance.
- Argos downloads do not expose byte progress through the library API. The installer now prints a heartbeat every 10 seconds.
- A sandbox-only Argos config-path error was observed at `C:\Users\asus\.config\argos-translate`; the user's normal PowerShell can run Argos and is the authoritative environment for package installation.
- Naive independent chunks can take 13-19 seconds on whisper-heavy ASMR sections.
- The browser audio capture path has not yet completed a live YouTube end-to-end verification.

## Next actions

1. Keep `base` as the default realtime model; do not use `tiny` as the recommended mode.
2. Reduce latency through capture and request pipelining without lowering recognition quality.
3. Add visible connection/recognition status to the extension popup.
4. Revisit Japanese translation only after a reliable language-package source is available.

## Run commands

Install remaining Argos packages:

```powershell
C:\text\.venv\Scripts\python.exe C:\text\实验\asmr_transcription\install_argos_languages.py
```

Start the Japanese service with translation:

```powershell
C:\text\.venv\Scripts\python.exe C:\text\实验\asmr_transcription\local_service.py --model base --language ja --translate
```

## 2026-08-15 开发记录

- 英语视频端到端链路已经打通：标签页音频捕获、本地 Whisper 识别、Argos 英译中和页面双语字幕均能运行。
- 修复 CORS 预检、WebM 增量块不可解码、Windows 临时文件锁和 offscreen 重复录音回调问题。
- 字幕支持双语、仅原文、仅中文三种模式；双语为上下排列。
- 字幕支持鼠标拖动位置，并可通过滚轮调整字号。
- 用户反馈英语字幕总体可用，但延迟明显；3 秒录音块已缩短为 2 秒。
- 测试 `tiny` 低延迟模型后，用户认为准确率不足。因此 `tiny` 只保留为实验选项，默认方案恢复为 `base`。
- 后续延迟优化将集中在音频分块、请求调度和流水线处理，不再以明显降低模型准确率为代价。
- 日语原文识别可用；日译中仍因 Argos `ja -> en` 语言包无法稳定下载而阻塞。

## 2026-08-16 开发记录

- 发现此前录音和识别为串行：每个 2 秒录音块必须等待识别及翻译结束后才开始下一段，造成音频空档和额外延迟。
- 改为流水线处理：上一段上传识别时，下一段立即开始录制。
- 本地服务响应新增 `processing_seconds`，PowerShell 日志会显示每个音频块的实际处理耗时。

## 停滞期评审 + M0/M1（本次会话）

架构评审结论与完整证据见 `ARCHITECTURE_REVIEW.md`，实测数据见 `BENCHMARK_RESULTS.md`。

- 完成架构评审。核心结论：当前实现本质是"离线文件转写包装成流式"，
  延迟下限被"块长度 + 每次调用固定开销"锁死；项目停滞的真正原因是没有可复现的离线验证回路。
- 用 `diag_call_cost.py` 证明每次 `transcribe()` 的固定成本 ≈ 0.9–1.1 秒，**与音频长度几乎无关**
  （base：2 秒音频 0.91s，10 秒音频 1.01s）。Whisper 的 encoder 永远处理补齐到 30 秒的 mel。
- 用 `diag_fallback.py` 定位 `WORKLOG` 中"某些块要 13–19 秒"的真正元凶是 **temperature 回退阶梯**
  （失败时用更高温度重解码整段，最多 7 遍），不是此前猜测的"耳语段落太难"。
  同一个 3 秒块：默认阶梯 6.59s → `temperature=[0.0]` 2.30s。但直接关掉会让该块从"空文本"变成复读幻觉，
  因此必须同时用 `repetition_penalty` / `no_repeat_ngram_size` 压制。
- **M0 完成**：
  - `git init` + `.gitignore`（排除 14MB 源视频与模型缓存，保留 sample wav 作为台架 fixture）；
    需要为本目录加 `safe.directory`（目录属主是之前的沙箱账户，与当前用户不同），仓库级身份设为 `asus/asus@localhost`。
  - `local_service.py` 重写：VAD 语音时长门控、收窄温度阶梯、内存流解码（不再落临时文件）、
    翻译移出模型锁、修掉 `finally` 中 `segments` 未绑定导致的 `NameError` 掩盖真实异常、启动预热 + `/health` 上报预热状态。
- **M1 完成**：离线回放台架 `tools/replay.py`（虚拟时钟，音频 1x 到达，计算耗时推进墙钟）
  + `tools/metrics.py` + `tools/offline_reference.py` + `tools/smoke_service.py` + `tools/diag_translate_cost.py`，
  以及 `app/` 分层骨架（events / audio.vad / asr 引擎 / pipelines 协议 + 固定分块基线）。
- **最重要的实测发现**：原型的 `cpu_ratio = 1.44`（识别 30 秒音频要烧 43 秒 CPU），
  端到端延迟 p50 **19.7 秒**、最坏 **27.3 秒**，且因为没有背压会单调落后、永不恢复。
  这才是"延迟明显"的量化答案，此前记录里的"至少一秒"严重低估了问题。
- 台架暴露的两个工程要求：单次 `cpu_ratio` 波动大（0.42–0.60），必须取中位数；
  一次病态调用耗时 1255 秒且不复现 —— **流水线必须有每次调用的硬时间预算与看门狗**。
- 修正了 `ARCHITECTURE_REVIEW.md` 第 5 节的推荐：双通道（方案 C）结构正确，
  但受 `base` 单次成本 1.10s 的结构性限制，**无法兑现"1.5 秒内看到正确文本"**。
  建议先做"换更低固定成本引擎"（SenseVoice / sherpa-onnx）的低成本判定实验，再决定 M2 建在哪个引擎上。

## 下一步

1. 判定实验：接一个非 Whisper 的流式引擎到 `Pipeline` 接口，用同一条 `replay.py` 命令对比
   `cpu_ratio` 与字错率，决定 M2 的引擎底座。
2. 在 `local_service.py` 与未来的流式内核里加每次调用的硬超时与丢弃策略。
3. M2：VAD 门控 + 开放段重解码 + LocalAgreement-2 增量提交 + 草稿/定稿两态。

## 换引擎判定实验 + M2（本次会话续）

### 判定实验：结论是换引擎

把 SenseVoice（sherpa-onnx）接到同一个 `Pipeline` 接口，**台架一行没改**（这正是 M1 的目的）。
完整数据见 `BENCHMARK_RESULTS.md` 第 10 节。

- 单次调用成本：SenseVoice 1 秒块 **0.038 秒**，base 是 **1.10 秒**，差 **29 倍**；
  而且 SenseVoice 的成本**随音频长度走**（30 秒整段 1.78 秒），没有 Whisper"补齐到 30 秒"的固定成本。
- 质量反而更好：SenseVoice 整段字错率 **0.022**（几乎与伪参考逐字相同），base 是 0.121。
- 因此第 5 节那两道把 base 逼死的约束自动松开，1.5 秒目标在改引擎后就已达成。
- 环境坑（已记录并自动处理）：GitHub Releases 直连约 2 KB/s（158 MB 要 20 小时以上），
  `gh-proxy.com` 776 KB/s、`hf-mirror.com` 669 KB/s、ModelScope 4.1 MB/s，已写进 `tools/setup_models.py`；
  PyPI 的 `sherpa-onnx-core` 只有 `py3-none-*` 标签（Python 3.13 可用），但 pip 联网解析会卡死，
  改用离线 wheel + `--no-index --find-links` 安装。

### M2 完成：VAD 驱动的"开放段重解码"

`app/pipelines/open_utterance.py`。句子边界由 VAD 决定（永不切在词中间），
开放期间每 `partial_step` 秒重解码整句出 partial，检测到静音则解码整句出 final。

**没有用 LocalAgreement-2**：它需要文本前缀与音频时间的对应关系才能裁剪缓冲区，
而 SenseVoice 既没有 `initial_prompt` 也没有词级时间戳；好在它单次成本只有约 0.05 秒/音频秒，
重解码整句本来就负担得起。

**达成情况（同一段 30 秒 ASMR，中位数，step=0.5s / sil=0.35s）：**

| 指标 | 目标 | 实测 |
|---|---|---|
| P95 延迟 | ≤ 1.5 秒 | **0.677 秒** |
| 最坏延迟 | — | 1.093 秒 |
| 首字延迟 P95 | — | 0.783 秒 |
| 定稿延迟 P95 | — | 1.076 秒 |
| 原文质量 | 不低于 base 整段（0.121） | **0.011**（好约 10 倍） |
| cpu_ratio | < 1 | 0.404（余 60% 给翻译） |

对照：停滞原型是 p50 19.7 秒 / 最坏 27.3 秒 / cpu_ratio 1.44 / 字错率 0.462。

- 修正了一个会把流式架构算错 5 倍的延迟口径问题：延迟必须按"音节第一次出现在屏幕上的延迟"算，
  不能拿最后一个事件的墙钟去惩罚整句。口径统一放在 `app/events.py::latency_views`，台架与报告共用，
  已验证两边数字完全一致；对固定分块会自动退化成原算法。
- 同引擎对照确认换引擎是必需的：M2 + Whisper base 能把质量从 0.341 提到 0.143，
  但 p95 仍是 3.2 秒——救得了质量，救不了延迟。

## 下一步（更新）

1. **M3**：把流式内核包成 WebSocket 服务（PCM 流进、partial/final 事件流出），替换
   `local_service.py` 的"每块一个 HTTP 请求"；扩展端改 AudioWorklet 取 PCM。
2. **M4**：partial/final 两态渲染、全屏挂载（当前 overlay 挂在 `documentElement`，全屏会消失）、
   设置持久化。
3. **M5**：改用 `opus-mt-ja-zh` CT2 直连翻译（已放弃 NLLB 与 Argos ja→en→zh 双重中转），
   独立 worker 不阻塞识别。
4. **M6**：端到端浏览器验证。

