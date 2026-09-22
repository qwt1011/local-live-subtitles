"""翻译引擎层。

为什么是 NLLB 而不是 opus-mt：**`Helsinki-NLP/opus-mt-ja-zh` 并不存在**
（只有 `opus-mt-tc-big-zh-ja` 这个反向模型），也没有 `opus-mt-mul-zh`。
所以"用 opus-mt 直连 ja->zh"在模型层面就不成立——这次是先查了再写代码，
而不是像 Argos 那次先假设再下载。

直连 ja->zh 的现实选择是 NLLB-200（多语言，`jpn_Jpan -> zho_Hans`）。
这里用已经转换好的 CTranslate2 int8 版本，所以：
- 不需要安装 transformers；
- 不需要在本机跑一遍转换（省掉约 2.4GB 的 fp32 下载）。
"""
