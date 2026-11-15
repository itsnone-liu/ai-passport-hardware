"""Speech shaping: markup removal and the read-aloud budget.

Both are plain rules on purpose. The tests pin the two failures that were
actually observed on the device: a reply spoken with `**` and backticks in it,
and a 42-second reading of a screen-sized answer.
"""
import unittest

from server.speech import CHARS_PER_SECOND, SpeechBudget, clean_for_speech


class TestCleanForSpeech(unittest.TestCase):
    def test_bold_and_inline_code_are_removed(self):
        out, _ = clean_for_speech("磁盘还剩 **31 GB**，命令是 `df -h`。")
        self.assertEqual(out, "磁盘还剩 31 GB，命令是 df -h。")

    def test_headings_bullets_and_quotes_lose_their_markers(self):
        out, _ = clean_for_speech("# 标题\n- 第一项\n- 第二项\n> 引用")
        self.assertNotIn("#", out)
        self.assertNotIn("- ", out)
        self.assertNotIn(">", out)
        self.assertIn("第一项", out)

    def test_a_fenced_block_becomes_one_placeholder(self):
        text = "结果如下：\n```\n/dev/sda1 117G 86G 31G 74% /\n```\n就这些。"
        out, state = clean_for_speech(text)
        self.assertFalse(state)
        self.assertIn("（细节我放屏幕上了）", out)
        self.assertNotIn("sda1", out)
        self.assertIn("就这些", out)

    def test_fence_state_survives_a_chunk_boundary(self):
        # the opening fence arrives in one chunk, the body in the next
        out1, state = clean_for_speech("看这个：\n```")
        self.assertTrue(state)
        out2, state = clean_for_speech("在这里面不该被念出来\n```\n好的。", in_fence=state)
        self.assertFalse(state)
        self.assertNotIn("不该被念出来", out1 + out2)

    def test_urls_and_paths_are_not_read_character_by_character(self):
        out, _ = clean_for_speech("详见 https://example.com/a/b 和 /root/.dsh/feishu/config.json")
        self.assertNotIn("https://", out)
        self.assertNotIn("/root/", out)
        self.assertIn("链接", out)
        self.assertIn("路径", out)

    def test_tables_do_not_become_pipe_noise(self):
        out, _ = clean_for_speech("| a | b |\n|---|---|\n| 1 | 2 |")
        self.assertNotIn("|", out)
        self.assertNotIn("---", out)


class TestSpeechBudget(unittest.TestCase):
    def test_short_answer_passes_through_untouched(self):
        b = SpeechBudget(45)
        self.assertEqual(b.take("还剩 31 GB。"), "还剩 31 GB。")
        self.assertFalse(b.truncated)

    def test_overflow_is_kept_not_lost(self):
        b = SpeechBudget(1.0)                     # floor of 20 chars (~4.5 s)
        text = "第一句话在这里说得挺长的。" * 3 + "最后这句应该被挤到屏幕上。"
        first = b.take(text)
        self.assertTrue(b.truncated)
        self.assertTrue(first.endswith("。"), f"cut mid-sentence: {first!r}")
        self.assertIn("屏幕上", b.overflow)

    def test_later_chunks_after_exhaustion_go_to_overflow(self):
        b = SpeechBudget(1.0)
        b.take("一" * 25)
        self.assertTrue(b.exhausted)
        self.assertEqual(b.take("后面这句不念"), "")
        self.assertIn("后面这句不念", b.overflow)

    def test_budget_scales_with_seconds(self):
        self.assertEqual(SpeechBudget(10).max_chars, int(10 * CHARS_PER_SECOND))
        self.assertGreater(SpeechBudget(45).max_chars, SpeechBudget(10).max_chars)

    def test_tail_note_only_when_truncated(self):
        b = SpeechBudget(45)
        b.take("短句。")
        self.assertEqual(b.tail_note(), "")
        b2 = SpeechBudget(1.0)
        b2.take("很长很长的一句话。" * 4)
        self.assertIn("屏幕", b2.tail_note())


if __name__ == "__main__":
    unittest.main(verbosity=2)
