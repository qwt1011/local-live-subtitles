"""翻译引擎工厂。与 app/asr/factory.py 同样的理由：服务与诊断脚本必须走同一条构造路径。"""

TRANSLATORS = ("instruct", "hymt", "nllb")

DEFAULT_MODEL = {
    # 指令模型是当前可用的路线，见 app/translate/instruct_local.py 的模块说明。
    "instruct": "Qwen2.5-0.5B-Instruct",
    # 专用翻译模型，评估中，见 app/translate/hymt_gguf.py。
    "hymt": "Hy-MT2-1.8B-GGUF/Hy-MT2-1.8B-Q4_K_M.gguf",
    # 本地转换的那一份（词表与权重一致）。注意：NLLB 路线整体仍被实测判定为不可用，
    # 保留它只是为了将来试"降级 CTranslate2"或"换推理引擎"时不必重下 2.35GB。
    "nllb": "nllb-200-distilled-600M-ct2-int8-local",
}


def create_translator(name, model=None, threads=None, warm_up=True, backend="ct2"):
    model = model or DEFAULT_MODEL[name]

    if name == "instruct":
        from .instruct_local import InstructTranslator
        translator = InstructTranslator(model, backend=backend, threads=threads)
    elif name == "hymt":
        from .hymt_gguf import HyMtTranslator
        translator = HyMtTranslator(model, threads=threads)
    elif name == "nllb":
        from .nllb_ct2 import NllbTranslator
        translator = NllbTranslator(model, threads=threads)
    else:
        raise SystemExit(f"unknown translator: {name}（可选：{', '.join(TRANSLATORS)}）")

    if warm_up:
        translator.warm_up()
    return translator
