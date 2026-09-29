"""OpenUtterancePipeline 的语气词过滤。

    python -m unittest tests.test_open_utterance
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.pipelines.base import Job  # noqa: E402
from app.pipelines.open_utterance import OpenUtterancePipeline, is_filler  # noqa: E402


class FakeEngine:
    name = "fake"

    def __init__(self, text):
        self.text = text

    def transcribe(self, pcm, language="ja"):
        return SimpleNamespace(text=self.text)


def final_job():
    return Job("final", 0.0, 1.0, np.zeros(16000, dtype=np.float32),
               meta={"segment_id": 0, "revision": 1})


class FillerTest(unittest.TestCase):
    def test_fillers(self):
        for text in ("う。", "へへ。", "へへへ。", "ふふふ。", "ふふふふ。", "うん。", "んー", "あ", "はは。", "え？"):
            self.assertTrue(is_filler(text), text)

    def test_meaningful_short_utterances_kept(self):
        for text in ("はい", "あら？", "そう", "大丈夫？", "なんてね。", "とか？", "その先よ。", "あなた", ""):
            self.assertFalse(is_filler(text), text)

    def test_pipeline_blanks_filler_final(self):
        pipeline = OpenUtterancePipeline(FakeEngine("へへ。"))
        [event] = pipeline.run_job(final_job())
        self.assertEqual(event.text, "")
        self.assertTrue(event.is_final)
        self.assertEqual(event.detail["filler"], "へへ。")
        self.assertEqual(pipeline.stats["fillers"], 1)

    def test_pipeline_keeps_speech(self):
        pipeline = OpenUtterancePipeline(FakeEngine("その先よ。"))
        [event] = pipeline.run_job(final_job())
        self.assertEqual(event.text, "その先よ。")
        self.assertNotIn("filler", event.detail)

    def test_filter_can_be_disabled(self):
        pipeline = OpenUtterancePipeline(FakeEngine("へへ。"), drop_fillers=False)
        [event] = pipeline.run_job(final_job())
        self.assertEqual(event.text, "へへ。")


if __name__ == "__main__":
    unittest.main()
