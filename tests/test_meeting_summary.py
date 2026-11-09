"""meeting_summary 的测试：说话人解析、发言提取与提示词。"""
import unittest

import meeting_summary

TRANSCRIPT = (
    "# 会议文字稿\n- 开始时间：2026-11-09 10:30\n- 时长：00:10:00\n- 说话人：说话人A、小王\n\n"
    "[00:00:01] 说话人A: 大家好\n[00:00:05] 小王: 你好，注意: 这里有冒号\n[00:00:09] 说话人A: 开始吧\n")
ANON = "# 会议文字稿\n- 开始时间：2026-11-09 10:30\n- 时长：00:00:09\n\n[00:00:01] 第一句\n[00:00:05] 第二句\n"


class SpeakerTests(unittest.TestCase):
    def test_list_speakers(self):
        self.assertEqual(meeting_summary.list_speakers(TRANSCRIPT), ["说话人A", "小王"])

    def test_list_speakers_requires_header(self):
        self.assertEqual(meeting_summary.list_speakers(ANON), [])
        self.assertEqual(meeting_summary.list_speakers(""), [])
        # 说话人行不在表头（首个空行之后）→ 不算
        self.assertEqual(meeting_summary.list_speakers("# x\n\n- 说话人：甲、乙\n[00:00:01] 甲: 嗨\n"), [])

    def test_speech_of_speaker(self):
        self.assertEqual(meeting_summary.speech_of(TRANSCRIPT, "说话人A"), "大家好\n\n开始吧")
        self.assertEqual(meeting_summary.speech_of(TRANSCRIPT, "小王"), "你好，注意: 这里有冒号")

    def test_speech_of_absent(self):
        self.assertEqual(meeting_summary.speech_of(TRANSCRIPT, "没有这个人"), "")
        self.assertEqual(meeting_summary.speech_of("", "甲"), "")

    def test_speech_of_anonymous(self):
        self.assertEqual(meeting_summary.speech_of(ANON, ""), "第一句\n\n第二句")
        self.assertEqual(meeting_summary.speech_of(ANON, "说话人A"), "")

    def test_empty_note_ignored(self):
        text = "# 会议文字稿\n- 说话人：甲\n\n（未识别到语音）\n"
        self.assertEqual(meeting_summary.speech_of(text, "甲"), "")
        self.assertEqual(meeting_summary.speech_of(text, ""), "")


SUSPECT = (
    "# 会议文字稿\n- 开始时间：2026-11-09 10:30\n- 时长：00:10:00\n"
    "- 说话人：张三、疑似张三、说话人A\n- 声纹匹配：张三 0.62、疑似张三 0.48\n\n"
    "[00:00:01] 张三: 我同意\n[00:00:05] 疑似张三: 我也同意\n[00:00:09] 说话人A: 好\n")


class SuspectMergeTests(unittest.TestCase):
    def test_list_speakers_merges_suspect(self):
        self.assertEqual(meeting_summary.list_speakers(SUSPECT), ["张三", "说话人A"])

    def test_speech_of_includes_suspect_parts(self):
        self.assertEqual(meeting_summary.speech_of(SUSPECT, "张三"), "我同意\n\n我也同意")
        self.assertEqual(meeting_summary.speech_of(SUSPECT, "说话人A"), "好")

    def test_speaker_has_suspect_flag(self):
        self.assertTrue(meeting_summary.speaker_has_suspect(SUSPECT, "张三"))
        self.assertFalse(meeting_summary.speaker_has_suspect(SUSPECT, "说话人A"))
        self.assertFalse(meeting_summary.speaker_has_suspect(SUSPECT, "没有这个人"))

    def test_sections_merge_and_keep_order(self):
        sections = meeting_summary.speech_sections(SUSPECT)
        self.assertEqual([n for n, _ in sections], ["张三", "说话人A"])
        self.assertEqual(sections[0][1], "我同意\n\n我也同意")

    def test_anonymous_untouched(self):
        self.assertEqual(meeting_summary.list_speakers(ANON), [])
        self.assertEqual(meeting_summary.speech_of(ANON, ""), "第一句\n\n第二句")
        self.assertFalse(meeting_summary.speaker_has_suspect(ANON, ""))


class PromptTests(unittest.TestCase):
    def test_sections_all_and_selected(self):
        self.assertEqual([n for n, _ in meeting_summary.speech_sections(TRANSCRIPT)], ["说话人A", "小王"])
        self.assertEqual([n for n, _ in meeting_summary.speech_sections(TRANSCRIPT, ["小王"])], ["小王"])
        self.assertEqual(meeting_summary.speech_sections(ANON), [("", "第一句\n\n第二句")])

    def test_unknown_speaker_value_error(self):
        with self.assertRaises(ValueError):
            meeting_summary.speech_sections(TRANSCRIPT, ["路人"])

    def test_build_summary_prompt(self):
        system, user = meeting_summary.build_summary_prompt(TRANSCRIPT)
        for word in ("会议纪要", "摘要", "要点", "待办", "决议"):
            self.assertIn(word, system + user)
        self.assertIn("说话人A", user)
        self.assertIn("大家好", user)
        self.assertIn("小王", user)
        _s, user2 = meeting_summary.build_summary_prompt(TRANSCRIPT, ["小王"])
        self.assertNotIn("大家好", user2)
        self.assertIn("你好", user2)

    def test_total_chars(self):
        self.assertEqual(meeting_summary.total_chars(meeting_summary.speech_sections(TRANSCRIPT)),
                         len("大家好\n\n开始吧") + len("你好，注意: 这里有冒号"))


if __name__ == "__main__":
    unittest.main()
