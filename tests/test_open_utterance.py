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
from app.pipelines.open_utterance import (  # noqa: E402
    OpenUtterancePipeline, ends_sentence, find_cut, is_filler)


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


def toks(spec):
    """'すぐに:1.0 。:1.5' → [(token, time)]，多字 token 按 0.1s 递增展开。"""
    out = []
    for item in spec.split("|"):
        token, stamp = item.rsplit(":", 1)
        out.append((token, float(stamp)))
    return out


# 「ごらんなさい。すぐに車に」：句中「。」前后间隔 0.8s
TWO_SENTENCES = toks("ご:0.1|ら:0.2|ん:0.3|な:0.4|さ:0.5|い:0.6|。:0.6|す:1.4|ぐ:1.5|に:1.6|車:1.7|に:1.8")


class FindCutTest(unittest.TestCase):
    def test_cut_between_sentences(self):
        cut = find_cut(TWO_SENTENCES, "ごらんなさいすぐに車")
        self.assertAlmostEqual(cut, 1.0)

    def test_needs_stable_prefix(self):
        # 上一次 partial 在边界后就不一样了：边界后的内容还没稳定，不切
        self.assertIsNone(find_cut(TWO_SENTENCES, "ごらんなさいで"))

    def test_trailing_period_is_not_a_boundary(self):
        tokens = TWO_SENTENCES[:7]
        self.assertIsNone(find_cut(tokens, "ごらんなさい"))

    def test_no_pause_no_cut(self):
        tight = [(t, s if i < 7 else s - 0.75) for i, (t, s) in enumerate(TWO_SENTENCES)]
        self.assertIsNone(find_cut(tight, "ごらんなさいすぐに車"))

    def test_too_short_prefix(self):
        tokens = toks("ね:0.1|。:0.1|す:1.0|ぐ:1.1|に:1.2")
        self.assertIsNone(find_cut(tokens, "ねすぐに"))

    def test_final_particle_then_space(self):
        tokens = toks("増:0.1|え:0.2|た:0.3|も:0.4|の:0.5|ね:0.6| :0.7|あ:1.2|な:1.3|が:1.4")
        self.assertAlmostEqual(find_cut(tokens, "増えたものねあなが"), 0.9)

    def test_final_particle_then_comma(self):
        tokens = toks("言:0.1|っ:0.2|て:0.3|た:0.4|も:0.5|の:0.6|ね:0.7|、:0.7|抵:1.3|抗:1.4|し:1.5")
        self.assertAlmostEqual(find_cut(tokens, "言ってたものね抵抗し"), 1.0)

    def test_plain_comma_is_not_a_boundary(self):
        tokens = toks("逃:0.1|げ:0.2|て:0.3|し:0.4|ま:0.5|う:0.6|、:0.6|場:1.3|面:1.4|に:1.5")
        self.assertIsNone(find_cut(tokens, "逃げてしまう場面に"))

    def test_plain_space_is_not_a_boundary(self):
        tokens = toks("助:0.1|け:0.2|を:0.3|呼:0.4|ん:0.5|だ:0.6| :0.7|意:1.2|味:1.3")
        self.assertIsNone(find_cut(tokens, "助けを呼んだ意味"))


class ScriptedEngine:
    """每次 transcribe 按调用顺序返回预设结果，记录收到的音频长度。"""
    name = "scripted"

    def __init__(self, results):
        self.results = list(results)
        self.lengths = []

    def transcribe(self, pcm, language="ja"):
        self.lengths.append(len(pcm) / 16000)
        text, tokens = self.results.pop(0) if self.results else ("", [])
        return SimpleNamespace(text=text, tokens=tokens)


def real_speech():
    """评测集 0230 的 52.35–56.45s 是一段 4 秒连续语音（VAD 需要真人声，正弦波不算语音）。"""
    import wave
    path = Path(__file__).resolve().parents[1] / "eval" / "audio" / "ja_asmr_0230.wav"
    with wave.open(str(path)) as handle:
        handle.setpos(int(52.35 * 16000))
        frames = handle.readframes(int(4.0 * 16000))
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768


class EarlyFinalTest(unittest.TestCase):
    def drive(self, early_final):
        partial = ("ごらんなさい。すぐに車に", TWO_SENTENCES)
        engine = ScriptedEngine([partial, partial] + [("ごらんなさい。", TWO_SENTENCES[:7])] * 5)
        pipeline = OpenUtterancePipeline(engine, partial_step=0.5, early_final=early_final)
        events = []
        # 连续语音，每次到达 0.5s，让流水线有机会一次次刷新 partial
        audio = real_speech()
        for step in range(8):
            pipeline.push_audio(audio[step * 8000:(step + 1) * 8000])
            while (job := pipeline.next_job(available_until=(step + 1) * 0.5, eof=False)) is not None:
                events.extend(pipeline.run_job(job))
        return pipeline, events

    def test_early_final_emitted(self):
        pipeline, events = self.drive(early_final=True)
        finals = [e for e in events if e.is_final]
        self.assertEqual(pipeline.stats["early_finals"], 1)
        self.assertEqual(len(finals), 1)
        self.assertTrue(finals[0].detail["early_final"])
        # 切点 = 开放段起点 + 1.0s；定稿只重解码到切点为止
        self.assertAlmostEqual(finals[0].audio_end - finals[0].audio_start, 1.0, places=2)
        # 之后新开一段，segment_id 递增
        later = [e for e in events if e.segment_id > finals[0].segment_id]
        self.assertTrue(later)

    def test_disabled_by_default(self):
        pipeline, events = self.drive(early_final=False)
        self.assertEqual(pipeline.stats["early_finals"], 0)
        self.assertFalse(any(e.is_final for e in events))


class EndsSentenceTest(unittest.TestCase):
    def test_sentence_like(self):
        for text in ("言ってたものね。", "大丈夫？", "その先よ。", "いいかしら。", "やめて！"):
            self.assertTrue(ends_sentence(text), text)

    def test_not_sentence_like(self):
        # SenseVoice 自动补的「。」不算句末
        for text in ("すぐに車に。", "逃げてしまう、", "抵抗しなかったの", ""):
            self.assertFalse(ends_sentence(text), text)


class AdaptiveSilenceTest(unittest.TestCase):
    def drive(self, text, adaptive_silence=0.2):
        """2 秒真人声后接 0.3 秒静音（< min_silence 0.35），每次到达 0.1s。"""
        engine = ScriptedEngine([(text, [])] * 20)
        pipeline = OpenUtterancePipeline(engine, partial_step=0.5,
                                         adaptive_silence=adaptive_silence)
        audio = np.concatenate([real_speech()[:32000], np.zeros(4800, dtype=np.float32)])
        events = []
        step = 1600
        for i in range(0, len(audio), step):
            pipeline.push_audio(audio[i:i + step])
            while (job := pipeline.next_job(available_until=(i + step) / 16000,
                                            eof=False)) is not None:
                events.extend(pipeline.run_job(job))
        return pipeline, events

    def test_short_pause_closes_after_final_particle(self):
        pipeline, events = self.drive("言ってたものね。")
        finals = [e for e in events if e.is_final]
        self.assertEqual(pipeline.stats["adaptive_finals"], 1)
        self.assertEqual(len(finals), 1)
        self.assertTrue(finals[0].detail["adaptive_final"])

    def test_short_pause_waits_mid_sentence(self):
        pipeline, events = self.drive("すぐに車に。")
        self.assertEqual(pipeline.stats["adaptive_finals"], 0)
        self.assertFalse(any(e.is_final for e in events))

    def test_disabled_by_default(self):
        pipeline, events = self.drive("言ってたものね。", adaptive_silence=None)
        self.assertFalse(any(e.is_final for e in events))


if __name__ == "__main__":
    unittest.main()
