# ASMR 本地识别基准

这个测试程序用于测量本机 CPU 运行 Whisper 的速度。它不会上传音频。

## 使用

```powershell
..\.\.venv\Scripts\python.exe -m pip install -r requirements.txt
..\.\.venv\Scripts\python.exe benchmark.py path\to\audio.wav --model base
```

首次运行会下载模型文件，模型缓存通常位于用户目录下。建议先使用 30-60 秒的音频片段。

支持 `wav`、`mp3`、`m4a` 等 ffmpeg 可读取的格式。

## 本地常驻服务

启动后模型只加载一次。`tiny` 适合低延迟，`base` 适合平衡，`small` 适合准确率：

```powershell
..\.\.venv\Scripts\python.exe local_service.py --model base --language ja
```

健康检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8765/health
```

服务接口：向 `http://127.0.0.1:8765/transcribe?language=ja` POST 一个 WAV 音频 body，返回 JSON 字幕片段。服务只监听本机回环地址，不接受局域网连接。

## 可选本地翻译

翻译层支持 Argos Translate。安装 Argos 及日语/英语到中文语言包后，用 `--translate` 启动服务；未安装时仍会正常返回原文，并在 JSON 中说明翻译不可用。
