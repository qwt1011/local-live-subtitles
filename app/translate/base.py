"""翻译引擎协议。

服务端只依赖这个协议，所以换后端（NLLB / 未来的小模型）不需要动管线。
"""

from typing import Protocol


class Translator(Protocol):
    name: str

    def warm_up(self) -> None:
        """把模型加载/惰性初始化的成本在开始计时之前付掉。

        实测过的教训：Argos 首次翻译要 15 秒（模型懒加载），
        不预热就会把这 15 秒算到用户的第一句话上。
        """

    def translate(self, text: str, source: str = "ja", target: str = "zh") -> str:
        ...
