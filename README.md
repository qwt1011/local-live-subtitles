# 本地实时字幕（Local Live Subtitles）

在 Chrome 里给正在播放的日语视频实时加中文字幕：识别和翻译都在本机 CPU 上跑，**音频不出本机**。
支持 YouTube 和用 Chrome 直接打开的本地视频/音频文件。

- 识别：Parakeet-TDT-CTC 0.6B 日语版（默认）或 SenseVoice，经 sherpa-onnx 推理
- 翻译：腾讯 Hy-MT2-1.8B（GGUF Q4_K_M，llama.cpp）
- 在 i5-13500H 笔记本上实测：原文在说完后约 0.5 秒出现，中文约 1 秒（p50）

> 状态：个人项目，日语 → 中文可日常使用。英语 → 中文还在开发中。目前只支持 Windows + Chrome。

## 工作方式

```
Chrome 标签页 ──tabCapture──▶ 扩展 offscreen（16 kHz PCM）──WebSocket──▶ 本地服务 127.0.0.1:8766
                                                                          │ VAD 断句 + 开放段重解码
                                                                          │ 识别（Parakeet / SenseVoice）
                                                                          │ 定稿 → 翻译（Hy-MT2，独立线程）
页面字幕覆盖层 ◀────────── 草稿 / 定稿 / 译文事件（同一句的 revision 单调递增）◀─┘
```

- 说话过程中每 0.5 秒刷新一次草稿，停顿 0.35 秒后定稿，定稿才送去翻译；
- 原文先出现，中文稍后在同一行原地补上，不会闪烁或回跳；
- 服务只监听回环地址，没有鉴权，**不要把端口暴露到局域网**。

## 安装

需要 Windows、Chrome 116+、Python 3.13（其他 3.10+ 版本未测）。模型约 2GB，运行时内存约 3GB。

```powershell
git clone <本仓库> asmr_transcription
cd asmr_transcription
python -m venv .venv                # 启动脚本依次找 .venv、..\..\.venv、PATH 里的 python
.venv\Scripts\activate

pip install -r requirements.txt
# llama-cpp-python 在 PyPI 只有源码包，没有编译器时用预编译 CPU wheel：
pip install llama-cpp-python==0.3.35 --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu

# 下载模型（内置镜像列表，按速度逐个尝试）
python tools\setup_models.py --engine parakeet-ja       # 识别，约 625MB
python tools\setup_models.py --engine sensevoice-2024   # 识别（多语言，英语用它），约 230MB
python tools\setup_models.py --engine hy-mt2            # 翻译，约 1.1GB
```

加载扩展：`chrome://extensions` → 打开「开发者模式」→「加载已解压的扩展程序」→ 选 `extension` 目录。

**一键启动（推荐）**：双击 `安装一键启动.bat`，然后在 `chrome://extensions` 里重新加载扩展。
之后在扩展弹窗里点「启动服务并开始字幕」即可，服务会在后台无窗口运行，闲置 30 分钟自动退出。
它只写当前用户的一条注册表项（`HKCU\Software\Google\Chrome\NativeMessagingHosts\local.live_subtitles`），
移动项目文件夹后需要重新运行一次；`卸载一键启动.bat` 可以清掉。

不装一键启动也可以：每次先双击 `启动字幕服务.bat`，看到 `流式字幕服务已启动` 后再用扩展。

**本地视频**：把 mp4/mp3 拖进 Chrome 就能用。第一次需要在 `chrome://extensions` → 本扩展「详细信息」里
打开「允许访问文件网址」。

## 使用

扩展弹窗里：

| 控件 | 作用 |
|---|---|
| 主按钮 | 服务没开时一键启动并开始字幕；字幕进行中时变成「停止字幕」 |
| 识别 | Parakeet（更准，默认）/ SenseVoice（更快）/ 混合（实验）。换模型会自动重启服务 |
| 显示 | 原文+中文 / 原文 / 中文 |
| 高级 | 实验开关、闲置自动关闭时长、查看服务日志、停止服务 |

字幕可以拖动位置，滚轮调字号，全屏时也会显示。

每次会话的逐句识别、翻译和延迟会写到 `runs/live/`（只保留最近 20 份，不入库），出问题时可以用它复盘。

## 目录

```
app/                 本地服务
  server.py          WebSocket 服务：PCM 进，草稿/定稿/译文事件出
  pipelines/         流式内核（open_utterance.py：VAD 断句 + 开放段重解码）
  asr/               识别引擎：Parakeet / SenseVoice（sherpa-onnx）、Whisper（对照）
  translate/         翻译引擎：Hy-MT2（默认）、Qwen2.5-0.5B（备选）
extension/           Chrome MV3 扩展：采集、弹窗、字幕覆盖层
tools/               启动脚本、一键启动宿主、模型下载、评测与诊断工具
tests/               单元测试与协议测试
eval/                评测集清单与参考文本（音频不入库）
runs/                评测结果（文档里各项数字的出处）
docs/                架构评审、实测记录、开发日志
legacy/              早期原型脚本
```

## 开发

```powershell
python tests\test_open_utterance.py      # 流水线单元测试
node tests\test_extension_logic.js       # 扩展纯逻辑测试
node tests\test_probe_service.js         # 需要 8766 上有服务在跑
node tests\test_ws_protocol.js           # 同上，另需 sample_0230_0300.wav
```

评测集的音频来自一个 YouTube 视频，版权原因不入库。要复现评测，先用 yt-dlp 下载 `eval/manifest.json` 里的源视频到项目根目录，
再运行 `python tools\build_eval.py` 切出片段，然后：

```powershell
python tools\eval_suite.py --engine sherpa --model parakeet-ja --tag my_run     # 离线回放：字错率 + 延迟
python tools\live_bench.py --tag my_live -- --asr parakeet                       # 真起服务按 1x 推流：含翻译的真实延迟
```

设计与取舍见 [DEVELOPMENT.md](DEVELOPMENT.md)，完整的实验过程见 [docs/](docs/)。

## 已知限制

- 英语：弹窗里可以选，但服务端目前只按日语识别和翻译。
- 长句要等说完才定稿翻译，10 秒以上的句子中文会晚到（长句分句的研究结论见 `docs/WORKLOG.md`）。
- 评测集只有 5 段、同一个说话人，参考文本是多模型交叉裁定、未经人工听写，只适合比较配置的相对好坏。
- 只测过 Windows 11 + Chrome 154。

## 许可证

代码以 [MIT](LICENSE) 许可证开源：可以自由使用、修改、分发，保留版权声明即可。

模型不随本仓库分发，由 `tools/setup_models.py` 从各自的发布页下载，使用时遵循各模型自己的许可证
（Parakeet：NVIDIA；SenseVoice：阿里 FunAudioLLM；Hy-MT2：腾讯）。
