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
