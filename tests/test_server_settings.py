"""Language-specific runtime settings and engine fallback regression tests."""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.server import EnginePool, Session, asr_settings


def options(**overrides):
    values = dict(language="ja", threads=None, partial_step=None, en_threads=None,
                  en_partial_step=None, en_engine="sherpa", en_model="parakeet-en-unified",
                  min_speech=0.4, min_silence=0.35, max_utterance=10, call_timeout=None,
                  keep_fillers=False, early_final=False, adaptive_silence=None)
    return SimpleNamespace(**(values | overrides))


class SettingsTest(unittest.TestCase):
    @patch("app.server.os.cpu_count", return_value=16)
    def test_defaults_only_apply_to_english_parakeet(self, _):
        args = options()
        self.assertEqual(asr_settings(args, "en", "sherpa", "parakeet-en-unified"), (3, 0.75))
        self.assertEqual(asr_settings(args, "ja", "sherpa", "parakeet-ja"), (None, 0.5))
        self.assertEqual(asr_settings(args, "en", "sensevoice", "sensevoice-2024"), (None, 0.5))

    @patch("app.server.os.cpu_count", return_value=4)
    def test_small_cpu_does_not_gain_threads(self, _):
        self.assertEqual(asr_settings(options(), "en", "sherpa", "parakeet-en-unified"), (1, 0.75))

    def test_global_overrides_remain_effective(self):
        args = options(threads=5, partial_step=0.5)
        self.assertEqual(asr_settings(args, "en", "sherpa", "parakeet-en-unified"), (5, 0.5))

    def test_english_overrides_do_not_affect_japanese(self):
        args = options(threads=5, partial_step=0.5, en_threads=2, en_partial_step=1.0)
        self.assertEqual(asr_settings(args, "en", "sherpa", "parakeet-en-unified"), (2, 1.0))
        self.assertEqual(asr_settings(args, "ja", "sherpa", "parakeet-ja"), (5, 0.5))

    @patch("app.server.os.cpu_count", return_value=16)
    @patch("app.server.create_engine")
    def test_pool_cache_and_language_switch(self, create, _):
        args = options()
        ja = SimpleNamespace(name="sherpa", model_name="parakeet-ja")
        en = SimpleNamespace(name="sherpa", model_name="sherpa-onnx-nemo-parakeet-unified-en")
        create.return_value = en
        pool = EnginePool(args, ja)
        session = Session(args, ja, pool=pool)
        self.assertEqual(session.pipeline.partial_step, 0.5)
        session.set_language("en")
        self.assertEqual(session.pipeline.partial_step, 0.75)
        create.assert_called_once_with("sherpa", "parakeet-en-unified", language="en", threads=3)
        session.set_language("ja")
        self.assertEqual(session.pipeline.partial_step, 0.5)
        session.set_language("en")
        self.assertEqual(create.call_count, 1)
        self.assertIsNone(args.partial_step)

    @patch("app.server.create_engine")
    def test_missing_parakeet_falls_back_to_sensevoice_settings(self, create):
        args = options()
        ja = SimpleNamespace(name="sherpa", model_name="parakeet-ja")
        sv = SimpleNamespace(name="sensevoice", model_name="sensevoice-2024")
        create.side_effect = [SystemExit("missing model"), sv]
        session = Session(args, ja, pool=EnginePool(args, ja))
        session.set_language("en")
        self.assertEqual(session.pipeline.partial_step, 0.5)
        self.assertIsNone(create.call_args.kwargs["threads"])

    def test_address_reset_rejects_inflight_and_queued_work(self):
        session = Session(options(), SimpleNamespace(name="sherpa", model_name="parakeet-ja"))
        self.assertFalse(session.address_consistency)
        session.apply_options({"address_consistency": True})
        old_epoch = session.address_epoch
        session.consistent_address("お兄さん。", "哥哥。", "ja", old_epoch, 0)
        session.received_samples = 16000 * 5
        session.reset_address_memory()
        for epoch, start in ((old_epoch, 6), (session.address_epoch, 0)):
            session.consistent_address("お兄さん。", "小哥。", "ja", epoch, start)
            self.assertEqual(session.address_memory.entries, {})
        session.consistent_address("お兄さん。", "小哥。", "ja", session.address_epoch, 6)
        self.assertEqual(session.address_memory.entries[("ja", "お兄さん")], "小哥")
        session.apply_options({"address_consistency": False})
        self.assertEqual(session.address_memory.entries, {})

    def test_translation_worker_keeps_one_model_call_per_sentence(self):
        from unittest.mock import Mock
        translator = Mock(name="translator")
        translator.name = "fake"
        translator.translate.side_effect = ["哥哥。", "小哥，过来。", "他的小哥。"]
        args = options(translate_context=0, translate_style="plain")
        session = Session(args, SimpleNamespace(name="sherpa", model_name="parakeet-ja"), translator)
        session.apply_options({"address_consistency": True})
        emitted = []
        session._emit = emitted.append
        for index, text in enumerate(("お兄さん。", "お兄さん、こっち。", "彼のお兄さん。")):
            session.translate_queue.put((index, 1, text, index * 2, index * 2 + 1))
        session.translate_queue.put(None)
        session._translate_loop()
        self.assertEqual(translator.translate.call_count, 3)
        self.assertEqual([e.translation for e in emitted], ["哥哥。", "哥哥，过来。", "他的小哥。"])
        self.assertEqual(emitted[1].detail["address_change"]["original"], "小哥，过来。")

    def test_disabled_worker_preserves_translation(self):
        from unittest.mock import Mock
        translator = Mock()
        translator.name = "fake"
        translator.translate.side_effect = ["哥哥。", "小哥。"]
        session = Session(options(translate_context=0, translate_style="plain"),
                          SimpleNamespace(name="sherpa", model_name="parakeet-ja"), translator)
        emitted = []
        session._emit = emitted.append
        for index in range(2):
            session.translate_queue.put((index, 1, "お兄さん。", index, index + 1))
        session.translate_queue.put(None)
        session._translate_loop()
        self.assertEqual([e.translation for e in emitted], ["哥哥。", "小哥。"])
        self.assertEqual(session.address_memory.entries, {})


if __name__ == "__main__":
    unittest.main()
