# 评测集

日语、英语各 5 段 ASMR，每段 60 秒，每种语言取自同一个参考视频。按整片 VAD 统计挑选，覆盖轻声、最轻、稀疏、密集/响亮几种情况，详见 `manifest.json`。
日语按字算错误率（CER），英语按词算（WER，大小写、标点、数字写法归一化后）。

英语的参考文本多一个来源：YouTube 自动字幕，与 whisper large-v3、small 三方交叉裁定。
英语源视频另有一份人工中文字幕版（B 站内嵌中字），用 `tools/ocr_subtitles.py` 提取到 `eval/zh_refs/`（不入库，是别人的翻译），
`tools/translation_vs_human.py` 用它比较整条链路的译文。

音频有版权，不入库。先用 yt-dlp 把 `manifest.json` 里 `sources` 的视频下载到项目根目录（文件名与 `path` 一致），然后：

```powershell
python tools\build_eval.py     # 切出 eval\audio\ 的全部片段，以及早期台架用的 sample_0230_0300.wav
python tools\eval_suite.py --engine sherpa --model parakeet-ja --tag my_run
python tools\eval_suite.py --engine sensevoice --model sensevoice-2024 --tag my_sv
```

## 参考文本分级

`eval/refs/<clip>.txt` 第二行的 `status:` 决定该片段是否计入正式字错率：

| status | 含义 | 计入 `cer` |
|---|---|---|
| `draft` | 单模型输出，未审校 | 否（只进 `cer_draft`） |
| `consensus` | 多来源交叉比对 + 语义裁定，**未经人工听写** | 是 |
| `verified` | 懂日语的人听过并修改过 | 是 |

目前 5 段都是 `consensus`。它的可信度足以比较两个配置的相对好坏，但**不是绝对准确率**。

## consensus 的生成方式

`python tools\consensus_ref.py` 为每段生成 `eval/review/<clip>.md`，包括：

- whisper large-v3 与 small 的整段转写（带上下文，beam 5）；
- 两者字符级对齐后的分歧表；
- SenseVoice 输出（只做对照）。

裁定规则：

1. 两者一致的部分直接采用。
2. 有分歧时，取语义通顺、与上下文一致的一方；两方都不通时，按惯用表达裁定，并在行尾注释里写明（例如「啖呵を切る」「眉唾」「好都合」）。
3. 语气词、笑声、只在一方出现的单音（「あ」「はい」）不计入。
4. 拿不准的地方在行尾注释加 `?`。
5. **被测系统 SenseVoice 不参与裁定。** 否则就是被测系统给自己出题，它的错误会被写进参考、变成"正确"。

已知局限：三路模型同时听错的地方（例如「は境に」→ 裁定为「を境に」），语义裁定也可能出错。发现参考有错时直接改 txt，并在注释里写明依据。
