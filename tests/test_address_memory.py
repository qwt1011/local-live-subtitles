"""Bounded terminology memory: ambiguous references must not become constraints."""

import sys
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.translate.address_memory import AddressMemory, address_terms
from app.translate.hymt_gguf import HyMtTranslator, PROMPT


class MemoryTest(unittest.TestCase):
    def test_natural_first_translation_then_reuse(self):
        memory = AddressMemory()
        self.assertEqual(memory.constraints("かわいいお兄さん。", "ja"), {})
        memory.observe("かわいいお兄さん。", "可爱的哥哥。", "ja")
        self.assertEqual(memory.constraints("お兄さん、こっち。", "ja"), {"お兄さん": "哥哥"})
        memory.observe("お兄さん。", "小哥。", "ja")
        self.assertEqual(memory.constraints("お兄さん。", "ja"), {"お兄さん": "哥哥"})

    def test_ambiguous_or_missing_alignment_is_not_learned(self):
        memory = AddressMemory()
        for output in ("你好。", "哥哥还是小哥？", ""):
            memory.observe("お兄さん。", output, "ja")
            self.assertEqual(memory.entries, {})

    def test_longest_chinese_variant(self):
        memory = AddressMemory()
        memory.observe("お兄さん。", "大哥哥。", "ja")
        self.assertEqual(memory.constraints("お兄さん。", "ja"), {"お兄さん": "大哥哥"})

    def test_quotes_third_person_objects_and_word_boundaries(self):
        for text, language in [("私のお兄さんです。", "ja"), ("君のお兄さん。", "ja"),
                               ("「お兄さん」と言った。", "ja"), ("He is a dummy.", "en"),
                               ("I bought a dummy.", "en"), ("dummy data", "en"),
                               ("dorky", "en"), ("My sweetheart is home.", "en")]:
            self.assertEqual(address_terms(text, language), [], text)

    def test_unrelated_text_has_no_constraints(self):
        memory = AddressMemory()
        memory.observe("Hey, dummy.", "嘿，笨蛋。", "en")
        self.assertEqual(memory.constraints("Hello.", "en"), {})
        self.assertEqual(memory.constraints("Hey, DUMMY.", "en"), {"dummy": "笨蛋"})
        self.assertEqual(memory.constraints("Hey, dork.", "en"), {})

    def test_clear_and_separate_instances(self):
        memory = AddressMemory()
        memory.observe("お兄さん。", "哥哥。", "ja")
        self.assertEqual(AddressMemory().entries, {})
        memory.clear()
        self.assertEqual(memory.entries, {})

    def test_postprocess_changes_only_one_aligned_address(self):
        memory = AddressMemory()
        self.assertEqual(memory.apply("かわいいお兄さん。", "可爱的哥哥。", "ja"), ("可爱的哥哥。", None))
        output, change = memory.apply("お兄さん、こっち。", "小哥，过来。", "ja")
        self.assertEqual(output, "哥哥，过来。")
        self.assertEqual(change["original"], "小哥，过来。")
        self.assertEqual(memory.apply("お兄さん、こっち。", "过来。", "ja"), ("过来。", None))

    def test_postprocess_does_not_touch_ambiguous_target(self):
        memory = AddressMemory()
        memory.apply("お兄さん。", "哥哥。", "ja")
        for text in ("他的小哥来了。", "小哥还是哥哥？", "小哥，小哥！", "不是小哥。", "他说小哥来了。"):
            self.assertEqual(memory.apply("お兄さん。", text, "ja"), (text, None))
        self.assertEqual(address_terms("お兄さんは医者です。", "ja"), [])

    def test_model_called_once_and_baseline_prompt_unchanged(self):
        translator = HyMtTranslator.__new__(HyMtTranslator)
        translator.llm = Mock()
        translator.llm.create_chat_completion.return_value = {"choices": [{"message": {"content": "你好"}}]}
        translator.max_new_tokens = 128
        translator.calls = 0
        translator.total_seconds = 0
        translator.translate("Hello.", source="en")
        translator.llm.create_chat_completion.assert_called_once()
        prompt = translator.llm.create_chat_completion.call_args.kwargs["messages"][0]["content"]
        self.assertEqual(prompt, PROMPT.format(target="中文", text="Hello."))


if __name__ == "__main__":
    unittest.main()
