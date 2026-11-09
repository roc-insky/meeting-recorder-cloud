"""meeting_store 的单元测试（只用临时目录，不碰真实 ~/Meetings）。"""
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

import common
import meeting_store
from meeting_store import MeetingNotFound


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / "Meetings"
        self.root.mkdir()

    def mk(self, name: str, *, transcript: bool = False) -> Path:
        d = self.root / name
        d.mkdir()
        if transcript:
            (d / "transcript.md").write_text("# 会议文字稿\n", encoding="utf-8")
        return d


class ListMeetingsTests(StoreTestCase):
    def test_empty_and_missing_root(self):
        self.assertEqual(meeting_store.list_meetings(self.root), [])
        self.assertEqual(meeting_store.list_meetings(self.root / "nope"), [])

    def test_sorted_new_to_old(self):
        self.mk("2026-11-08_0900")
        self.mk("2026-11-09_1030", transcript=True)
        self.mk("2026-11-09_1030_2")
        self.mk("2026-11-09_0800")
        ids = [m["id"] for m in meeting_store.list_meetings(self.root)]
        self.assertEqual(ids, ["2026-11-09_1030_2", "2026-11-09_1030", "2026-11-09_0800", "2026-11-08_0900"])

    def test_fields(self):
        self.mk("a", transcript=True)
        self.mk("b")
        items = {m["id"]: m for m in meeting_store.list_meetings(self.root)}
        self.assertEqual(items["a"], {"id": "a", "title": "a", "has_transcript": True})
        self.assertEqual(items["b"], {"id": "b", "title": "b", "has_transcript": False})

    def test_title_from_meta(self):
        d = self.mk("a")
        meeting_store.set_title(d, "周会")
        self.assertEqual(meeting_store.list_meetings(self.root)[0]["title"], "周会")

    def test_ignores_files_hidden_and_symlinks(self):
        self.mk("ok")
        self.mk(".hidden")
        (self.root / "file.txt").write_text("x", encoding="utf-8")
        outside = Path(self._tmp.name) / "outside"
        outside.mkdir()
        os.symlink(outside, self.root / "link")
        ids = [m["id"] for m in meeting_store.list_meetings(self.root)]
        self.assertEqual(ids, ["ok"])

    def test_transcript_dir_is_not_transcript(self):
        d = self.mk("a")
        (d / "transcript.md").mkdir()
        self.assertFalse(meeting_store.list_meetings(self.root)[0]["has_transcript"])

    def test_corrupt_meta_falls_back_to_dir_name(self):
        d = self.mk("2026-11-09_1030")
        (d / "meta.json").write_text("{not json", encoding="utf-8")
        self.assertEqual(meeting_store.get_title(d), "2026-11-09_1030")
        self.assertEqual(meeting_store.list_meetings(self.root)[0]["title"], "2026-11-09_1030")


class ListIncompleteTests(StoreTestCase):
    def test_four_kinds_and_order(self):
        a = self.mk("2026-11-09_1030")
        (a / "meeting.wav").write_bytes(b"RIFF")
        b = self.mk("2026-11-09_0900", transcript=True)
        (b / "meeting.wav").write_bytes(b"RIFF")
        self.mk("2026-11-08_1700", transcript=True)          # 有稿无音频
        c = self.mk("2026-11-08_0900")                       # 有音频无稿，但正在转写
        (c / "meeting.wav").write_bytes(b"RIFF")
        self.mk("2026-11-07_0900")                            # 空目录
        self.assertEqual(meeting_store.list_incomplete(self.root, {"2026-11-08_0900"}),
                         ["2026-11-09_1030"])
        self.assertEqual(meeting_store.list_incomplete(self.root, []),
                         ["2026-11-09_1030", "2026-11-08_0900"])
        self.assertEqual(meeting_store.list_incomplete(self.root / "missing", []), [])

    def test_never_opens_files(self):
        d = self.mk("a")
        (d / "meeting.wav").write_bytes(b"RIFF")
        with mock.patch("builtins.open", side_effect=AssertionError("list_incomplete 不允许打开文件")):
            self.assertEqual(meeting_store.list_incomplete(self.root, ()), ["a"])


class ResolveTests(StoreTestCase):
    def test_valid(self):
        d = self.mk("2026-11-09_1030")
        self.assertEqual(meeting_store.resolve(self.root, "2026-11-09_1030"), d.resolve())

    def test_rejects_bad_ids(self):
        self.mk("a")
        outside = Path(self._tmp.name) / "x"
        outside.mkdir()
        bad = ["", "..", ".", "../x", "a/..", "a/b", "/etc", str(outside), "a\x00b", ".hidden", "a\\b", None, 5]
        for bid in bad:
            with self.subTest(bid=bid):
                with self.assertRaises(MeetingNotFound):
                    meeting_store.resolve(self.root, bid)

    def test_nonexistent(self):
        with self.assertRaises(MeetingNotFound):
            meeting_store.resolve(self.root, "nope")

    def test_file_not_dir(self):
        (self.root / "f").write_text("x", encoding="utf-8")
        with self.assertRaises(MeetingNotFound):
            meeting_store.resolve(self.root, "f")

    def test_symlink_escape(self):
        outside = Path(self._tmp.name) / "outside"
        outside.mkdir()
        os.symlink(outside, self.root / "evil")
        with self.assertRaises(MeetingNotFound):
            meeting_store.resolve(self.root, "evil")

    def test_symlink_to_sibling_meeting_rejected(self):
        self.mk("real")
        os.symlink(self.root / "real", self.root / "alias")
        with self.assertRaises(MeetingNotFound):
            meeting_store.resolve(self.root, "alias")

    def test_result_inside_root(self):
        self.mk("a")
        p = meeting_store.resolve(self.root, "a")
        p.relative_to(self.root.resolve())


class TitleTests(StoreTestCase):
    def test_default_is_dir_name(self):
        d = self.mk("a")
        self.assertEqual(meeting_store.get_title(d), "a")

    def test_roundtrip_and_meta_file(self):
        d = self.mk("a")
        meeting_store.set_title(d, "产品评审 ✓")
        self.assertEqual(meeting_store.get_title(d), "产品评审 ✓")
        self.assertEqual(json.loads((d / "meta.json").read_text(encoding="utf-8"))["title"], "产品评审 ✓")

    def test_preserves_other_meta_keys(self):
        d = self.mk("a")
        (d / "meta.json").write_text(json.dumps({"other": 1}), encoding="utf-8")
        meeting_store.set_title(d, "x")
        self.assertEqual(json.loads((d / "meta.json").read_text(encoding="utf-8")), {"other": 1, "title": "x"})

    def test_overwrites_corrupt_meta(self):
        d = self.mk("a")
        (d / "meta.json").write_text("garbage", encoding="utf-8")
        meeting_store.set_title(d, "好")
        self.assertEqual(meeting_store.get_title(d), "好")

    def test_100_ok_101_rejected(self):
        d = self.mk("a")
        meeting_store.set_title(d, "字" * 100)
        with self.assertRaises(ValueError) as cm:
            meeting_store.set_title(d, "字" * 101)
        self.assertIn("100", str(cm.exception))
        self.assertEqual(meeting_store.get_title(d), "字" * 100)

    def test_rejects_newlines_empty_non_str(self):
        d = self.mk("a")
        for bad in ("a\nb", "a\rb", "a\r\nb", "", "   ", None, 5):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    meeting_store.set_title(d, bad)
        self.assertFalse((d / "meta.json").exists())

    def test_title_not_silently_altered(self):
        d = self.mk("a")
        meeting_store.set_title(d, " 两端空格 ")
        self.assertEqual(meeting_store.get_title(d), " 两端空格 ")

    def test_meta_with_invalid_title_value_falls_back(self):
        d = self.mk("a")
        for bad in ({"title": 5}, {"title": ""}, {"title": "x\ny"}, {"title": "字" * 101}, [1], "s"):
            (d / "meta.json").write_text(json.dumps(bad), encoding="utf-8")
            with self.subTest(bad=bad):
                self.assertEqual(meeting_store.get_title(d), "a")

    def test_no_temp_files_left(self):
        d = self.mk("a")
        meeting_store.set_title(d, "x")
        self.assertEqual(sorted(p.name for p in d.iterdir()), ["meta.json"])


class AnnotationTests(StoreTestCase):
    def test_empty(self):
        d = self.mk("a")
        self.assertEqual(meeting_store.list_annotations(d), [])

    def test_decimals_preserved(self):
        d = self.mk("a")
        meeting_store.add_annotation(d, "note", 1.2345, "要点")
        got = meeting_store.list_annotations(d)
        self.assertEqual(got, [{"kind": "note", "t": 1.2345, "text": "要点"}])

    def test_returns_added_record(self):
        d = self.mk("a")
        rec = meeting_store.add_annotation(d, "highlight", 3, "x")
        self.assertEqual(rec, {"kind": "highlight", "t": 3, "text": "x"})

    def test_sorted_by_t_then_insertion(self):
        d = self.mk("a")
        meeting_store.add_annotation(d, "note", 5, "e")
        meeting_store.add_annotation(d, "comment", 1, "b1")
        meeting_store.add_annotation(d, "highlight", 1, "b2")
        meeting_store.add_annotation(d, "note", 0, "a")
        self.assertEqual([a["text"] for a in meeting_store.list_annotations(d)], ["a", "b1", "b2", "e"])

    def test_kinds(self):
        d = self.mk("a")
        for kind in ("note", "highlight", "comment"):
            meeting_store.add_annotation(d, kind, 0, "x")
        for bad in ("other", "", None, 5, "Note"):
            with self.subTest(kind=bad):
                with self.assertRaises(ValueError):
                    meeting_store.add_annotation(d, bad, 0, "x")

    def test_bad_t(self):
        d = self.mk("a")
        for bad in (True, False, -0.1, -1, float("nan"), float("inf"), float("-inf"), "1", None, [1]):
            with self.subTest(t=bad):
                with self.assertRaises(ValueError):
                    meeting_store.add_annotation(d, "note", bad, "x")
        self.assertEqual(meeting_store.list_annotations(d), [])

    def test_zero_t_ok(self):
        d = self.mk("a")
        meeting_store.add_annotation(d, "note", 0, "x")
        meeting_store.add_annotation(d, "note", 0.0, "y")
        self.assertEqual(len(meeting_store.list_annotations(d)), 2)

    def test_bad_text(self):
        d = self.mk("a")
        for bad in ("", "   ", None, 5, "字" * 2001):
            with self.subTest(text=bad if not isinstance(bad, str) else len(bad)):
                with self.assertRaises(ValueError):
                    meeting_store.add_annotation(d, "note", 1, bad)
        meeting_store.add_annotation(d, "note", 1, "字" * 2000)
        self.assertEqual(len(meeting_store.list_annotations(d)), 1)

    def test_multiline_text_allowed_and_unaltered(self):
        d = self.mk("a")
        meeting_store.add_annotation(d, "note", 1, " 第一行\n第二行 ")
        self.assertEqual(meeting_store.list_annotations(d)[0]["text"], " 第一行\n第二行 ")

    def test_corrupt_file_tolerated_and_data_kept_aside(self):
        d = self.mk("a")
        (d / "annotations.json").write_text("{oops", encoding="utf-8")
        self.assertEqual(meeting_store.list_annotations(d), [])
        meeting_store.add_annotation(d, "note", 1, "新")
        self.assertEqual([a["text"] for a in meeting_store.list_annotations(d)], ["新"])
        backups = sorted(d.glob("annotations.json.*.corrupt"))
        self.assertEqual([b.read_text(encoding="utf-8") for b in backups], ["{oops"])

    def _backups(self, d):
        return sorted(d.glob("annotations.json.*.corrupt"))

    def test_wrong_shape_json_is_backed_up_before_replacement(self):
        d = self.mk("a")
        original = json.dumps({"annotations": [{"kind": "note", "t": 1, "text": "珍贵"}]}, ensure_ascii=False)
        (d / "annotations.json").write_text(original, encoding="utf-8")
        self.assertEqual(meeting_store.list_annotations(d), [])      # 只读不改动
        self.assertEqual(self._backups(d), [])
        meeting_store.add_annotation(d, "note", 1, "新")
        backups = self._backups(d)
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), original)

    def test_rejected_entries_are_backed_up_before_rewrite(self):
        d = self.mk("a")
        original = json.dumps([{"kind": "note", "t": 2, "text": "ok"}, {"kind": "bad", "t": 1, "text": "留着"}],
                              ensure_ascii=False)
        (d / "annotations.json").write_text(original, encoding="utf-8")
        meeting_store.add_annotation(d, "note", 3, "新")
        self.assertEqual([a["text"] for a in meeting_store.list_annotations(d)], ["ok", "新"])
        self.assertEqual([b.read_text(encoding="utf-8") for b in self._backups(d)], [original])

    def test_clean_file_makes_no_backup(self):
        d = self.mk("a")
        meeting_store.add_annotation(d, "note", 1, "甲")
        meeting_store.add_annotation(d, "note", 2, "乙")
        self.assertEqual(self._backups(d), [])

    def test_two_successive_corruptions_keep_two_distinct_backups(self):
        d = self.mk("a")
        (d / "annotations.json").write_text("{first", encoding="utf-8")
        meeting_store.add_annotation(d, "note", 1, "甲")
        (d / "annotations.json").write_text("[1, 2,", encoding="utf-8")
        meeting_store.add_annotation(d, "note", 2, "乙")
        backups = self._backups(d)
        self.assertEqual(len(backups), 2)
        self.assertEqual(sorted(b.read_text(encoding="utf-8") for b in backups), ["[1, 2,", "{first"])

    def test_same_timestamp_never_overwrites_backup(self):
        d = self.mk("a")
        with mock.patch("common.datetime") as dt:
            dt.now.return_value = datetime(2026, 11, 9, 10, 0, 0, 0)
            for i in range(3):
                (d / "annotations.json").write_text("坏%d{" % i, encoding="utf-8")
                meeting_store.add_annotation(d, "note", i, "x")
        self.assertEqual(sorted(b.read_text(encoding="utf-8") for b in self._backups(d)), ["坏0{", "坏1{", "坏2{"])

    def test_backup_failure_aborts_write_and_keeps_original(self):
        d = self.mk("a")
        (d / "annotations.json").write_text("{oops", encoding="utf-8")
        with mock.patch("common.shutil.copyfile", side_effect=OSError("磁盘满")):
            with self.assertRaises(common.DataProtectionError) as cm:
                meeting_store.add_annotation(d, "note", 1, "新")
        self.assertIn("备份", str(cm.exception))
        self.assertEqual((d / "annotations.json").read_text(encoding="utf-8"), "{oops")
        self.assertEqual(self._backups(d), [])

    def test_malformed_items_skipped(self):
        d = self.mk("a")
        (d / "annotations.json").write_text(json.dumps([
            {"kind": "note", "t": 2, "text": "ok"},
            {"kind": "bad", "t": 1, "text": "x"},
            {"kind": "note", "t": True, "text": "x"},
            "str",
            {"kind": "note", "t": 1, "text": ""},
        ]), encoding="utf-8")
        self.assertEqual([a["text"] for a in meeting_store.list_annotations(d)], ["ok"])

    def test_huge_integer_timestamp_skipped_not_crash(self):
        """401 位整数的 t 在 math.isfinite 里会 OverflowError：读取应跳过该条而不是崩溃，且写入前备份原件。"""
        d = self.mk("a")
        huge = "1" + "0" * 400
        (d / "annotations.json").write_text(
            '[{"kind": "note", "t": %s, "text": "huge"}, {"kind": "note", "t": 2, "text": "ok"}]' % huge,
            encoding="utf-8")
        self.assertEqual([a["text"] for a in meeting_store.list_annotations(d)], ["ok"])
        meeting_store.add_annotation(d, "note", 3, "new")
        self.assertEqual([a["text"] for a in meeting_store.list_annotations(d)], ["ok", "new"])
        backups = self._backups(d)
        self.assertEqual(len(backups), 1)
        self.assertIn(huge, backups[0].read_text(encoding="utf-8"))

    def test_huge_integer_timestamp_rejected_on_add(self):
        d = self.mk("a")
        with self.assertRaises(ValueError):
            meeting_store.add_annotation(d, "note", 10 ** 400, "x")

    def test_concurrent_adds_no_loss_and_valid_json(self):
        d = self.mk("a")
        n = 20

        def work(i):
            meeting_store.add_annotation(d, "note", i, f"n{i}")

        threads = [threading.Thread(target=work, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        got = meeting_store.list_annotations(d)
        self.assertEqual([a["t"] for a in got], list(range(n)))
        json.loads((d / "annotations.json").read_text(encoding="utf-8"))
        self.assertEqual([p.name for p in d.iterdir() if p.name != "annotations.json"], [])

    def test_missing_dir_raises_not_found(self):
        with self.assertRaises(MeetingNotFound):
            meeting_store.add_annotation(self.root / "nope", "note", 1, "x")


class DeleteTests(StoreTestCase):
    def test_delete_removes_dir(self):
        d = self.mk("a", transcript=True)
        (d / "sub").mkdir()
        (d / "sub" / "f").write_text("x", encoding="utf-8")
        meeting_store.delete_meeting(self.root, "a")
        self.assertFalse(d.exists())
        self.assertTrue(self.root.exists())

    def test_delete_nonexistent(self):
        with self.assertRaises(MeetingNotFound):
            meeting_store.delete_meeting(self.root, "nope")

    def test_delete_rejects_traversal(self):
        victim = Path(self._tmp.name) / "victim"
        victim.mkdir()
        with self.assertRaises(MeetingNotFound):
            meeting_store.delete_meeting(self.root, "../victim")
        self.assertTrue(victim.exists())

    def test_delete_symlink_refused_target_kept(self):
        outside = Path(self._tmp.name) / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("k", encoding="utf-8")
        os.symlink(outside, self.root / "evil")
        with self.assertRaises(MeetingNotFound):
            meeting_store.delete_meeting(self.root, "evil")
        self.assertTrue((outside / "keep.txt").exists())
        self.assertTrue((self.root / "evil").is_symlink())


class ParseHeaderTests(unittest.TestCase):
    def test_with_speakers(self):
        text = "# 会议文字稿\n- 开始时间：2026-11-09 10:30\n- 时长：00:10:00\n- 说话人：说话人A、张三、说话人C\n\n[00:00:01] 说话人A: 你好\n"
        self.assertEqual(meeting_store.parse_header(text)["speakers"], ["说话人A", "张三", "说话人C"])

    def test_without_speakers_line(self):
        text = "# 会议文字稿\n- 开始时间：2026-11-09 10:30\n- 时长：00:10:00\n\n[00:00:01] 你好\n"
        self.assertEqual(meeting_store.parse_header(text)["speakers"], [])

    def test_comma_separator_and_whitespace(self):
        self.assertEqual(meeting_store.parse_header("- 说话人： 甲 , 乙，丙\n")["speakers"], ["甲", "乙", "丙"])

    def test_empty_values_dropped_and_deduped(self):
        self.assertEqual(meeting_store.parse_header("- 说话人：甲、、乙、甲\n")["speakers"], ["甲", "乙"])

    def test_empty_line_value(self):
        self.assertEqual(meeting_store.parse_header("- 说话人：\n")["speakers"], [])

    def test_only_header_region_not_body(self):
        text = "# 会议文字稿\n- 时长：00:01:00\n\n[00:00:01] - 说话人：伪造、假\n- 说话人：正文里的\n"
        self.assertEqual(meeting_store.parse_header(text)["speakers"], [])

    def test_crlf(self):
        self.assertEqual(meeting_store.parse_header("# x\r\n- 说话人：甲、乙\r\n")["speakers"], ["甲", "乙"])

    def test_empty_text(self):
        self.assertEqual(meeting_store.parse_header("")["speakers"], [])

    def test_matches_real_render(self):
        import transcribe
        from datetime import datetime
        text = transcribe.render([{"start": 0, "end": 1, "text": "嗨"}], started=datetime(2026, 11, 9, 10, 30),
                                 duration=61, speakers=["说话人A", "说话人B"])
        self.assertEqual(meeting_store.parse_header(text)["speakers"], ["说话人A", "说话人B"])


class VoiceScoresHeaderTests(unittest.TestCase):
    def test_old_format_has_empty_scores(self):
        h = meeting_store.parse_header("- 说话人：甲、乙\n\n[00:00:01] 甲: 嗨\n")
        self.assertEqual(h["speakers"], ["甲", "乙"])
        self.assertEqual(h["voice_scores"], {})

    def test_new_format_and_gray_label(self):
        text = ("# 会议文字稿\n- 说话人：张三、疑似李四、说话人A\n"
                "- 声纹匹配：张三 0.62、疑似李四 0.48\n\n[00:00:01] 张三: 嗨\n")
        h = meeting_store.parse_header(text)
        self.assertEqual(h["speakers"], ["张三", "疑似李四", "说话人A"])
        self.assertEqual(h["voice_scores"], {"张三": 0.62, "疑似李四": 0.48})

    def test_malformed_scores_ignored(self):
        text = "- 声纹匹配：张三 0.62、坏行、李四 abc、王五 1.5\n\n"
        self.assertEqual(meeting_store.parse_header(text)["voice_scores"], {"张三": 0.62, "王五": 1.5})

    def test_scores_line_only_read_in_header(self):
        text = "- 说话人：甲\n\n- 声纹匹配：甲 0.9\n"
        self.assertEqual(meeting_store.parse_header(text)["voice_scores"], {})


if __name__ == "__main__":
    unittest.main()
