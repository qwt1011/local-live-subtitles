"""识别引擎工厂。

台架（tools/replay.py）和服务（app/server.py）都必须用同一条构造路径，
否则"台架测出来的数字"和"服务里的行为"会悄悄分叉——这正是评审里
"服务一套参数、台架另一套"那类问题的根源。
"""

from .faster_whisper_engine import WhisperEngine

ENGINES = ("whisper", "sensevoice", "sherpa")

DEFAULT_MODEL = {
    "whisper": "base",
    "sensevoice": "sensevoice-2024",
    "sherpa": "parakeet-ja",
}

# 复现"停滞版本"参数用的旧配置，只在对照实验里使用。
LEGACY_WHISPER_KWARGS = {
    "temperature": [0.0, 0.2, 0.4, 0.6, 0.8, 1.0],
    "repetition_penalty": 1.0,
    "no_repeat_ngram_size": 0,
    "condition_on_previous_text": True,
}


def create_engine(engine_name, model_name=None, language="ja", threads=None,
                  legacy_params=False, warm_up=True):
    model_name = model_name or DEFAULT_MODEL[engine_name]

    if engine_name == "sensevoice":
        if legacy_params:
            raise SystemExit("--legacy-params 只对 whisper 有意义")
        from .sensevoice_engine import SenseVoiceEngine
        engine = SenseVoiceEngine(model_name, num_threads=threads, language=language)
    elif engine_name == "sherpa":
        from .sherpa_offline_engine import SherpaOfflineEngine
        engine = SherpaOfflineEngine(model_name, num_threads=threads, language=language)
    elif engine_name == "whisper":
        overrides = LEGACY_WHISPER_KWARGS if legacy_params else {}
        engine = WhisperEngine(model_name, **overrides)
    else:
        raise SystemExit(f"unknown engine: {engine_name}（可选：{', '.join(ENGINES)}）")

    if warm_up:
        engine.warm_up(language)
    return engine
