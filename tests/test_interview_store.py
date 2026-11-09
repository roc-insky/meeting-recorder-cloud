"""interview_store 的测试：职位校验、原子写与重载。"""
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import common
import interview_store
from interview_store import JobNotFound, JobStore


class StoreCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / "jobs.json"
        self.store = JobStore(self.path)


class JobValidationTests(StoreCase):
    def test_job_validation(self):
        bad = [
            ("管理", "标题", "要求"), (None, "标题", "要求"), ("后端", "x" * 101, "要求"),
            ("后端", "两\n行", "要求"), ("后端", "回车\r", "要求"), ("后端", "  ", "要求"),
            ("后端", 5, "要求"), ("后端", "标题", "y" * 10001), ("后端", "标题", 7),
        ]
        for type_, title, req in bad:
            with self.subTest(type=type_, title=str(title)[:5], req=str(req)[:5]):
                with self.assertRaises(ValueError) as cm:
                    self.store.create(type_, title, req)
                self.assertRegex(str(cm.exception), r"[\u4e00-\u9fff]")
        self.assertEqual(self.store.list(), [])
        self.assertFalse(self.path.exists())

    def test_boundaries_ok(self):
        for t in interview_store.JOB_TYPES:
            self.store.create(t, "x" * 100, "y" * 10000)
        self.assertEqual(interview_store.JOB_TYPES, ("前端", "后端", "客户端", "测试"))
        self.assertEqual(len(self.store.list()), 4)

    def test_requirements_multiline_ok(self):
        job = self.store.create("前端", "标题", "第一行\n第二行")
        self.assertEqual(job["requirements"], "第一行\n第二行")


class JobCrudTests(StoreCase):
    def test_create_fields(self):
        job = self.store.create("后端", "Python 工程师", "熟悉 FastAPI")
        self.assertRegex(job["id"], r"^[0-9a-f]{32}$")
        self.assertEqual((job["type"], job["title"], job["requirements"]), ("后端", "Python 工程师", "熟悉 FastAPI"))
        self.assertEqual(job["created_at"], job["updated_at"])
        self.assertIn("T", job["created_at"])

    def test_job_store_atomic_and_reload(self):
        a = self.store.create("后端", "甲", "要求甲")
        b = self.store.create("前端", "乙", "要求乙")
        again = JobStore(self.path)
        self.assertEqual(again.list(), [a, b])
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertIsInstance(data, list)
        self.assertEqual(sorted(p.name for p in self.path.parent.iterdir()), ["jobs.json"])  # 无临时文件残留
        with mock.patch("common.atomic_write_text", side_effect=OSError("磁盘满")):
            with self.assertRaises(OSError):
                self.store.create("测试", "丙", "")
        self.assertEqual(JobStore(self.path).list(), [a, b])       # 写失败不破坏旧内容

    def test_uses_atomic_write(self):
        with mock.patch("common.atomic_write_text") as w:
            self.store.create("测试", "丙", "")
        self.assertEqual(w.call_count, 1)

    def test_update(self):
        job = self.store.create("后端", "旧", "旧要求")
        new = self.store.update(job["id"], "前端", "新", "新要求")
        self.assertEqual((new["type"], new["title"], new["requirements"]), ("前端", "新", "新要求"))
        self.assertEqual(new["created_at"], job["created_at"])
        self.assertEqual(JobStore(self.path).list(), [new])

    def test_update_invalid_keeps_old(self):
        job = self.store.create("后端", "旧", "旧要求")
        with self.assertRaises(ValueError):
            self.store.update(job["id"], "后端", "x" * 101, "新")
        self.assertEqual(self.store.list(), [job])

    def test_unknown_id(self):
        with self.assertRaises(JobNotFound):
            self.store.update("nope", "后端", "a", "b")
        with self.assertRaises(JobNotFound):
            self.store.delete("nope")
        with self.assertRaises(JobNotFound):
            self.store.get("nope")

    def test_delete(self):
        a = self.store.create("后端", "甲", "")
        b = self.store.create("后端", "乙", "")
        self.store.delete(a["id"])
        self.assertEqual(JobStore(self.path).list(), [b])
        self.assertEqual(self.store.get(b["id"]), b)

    def test_corrupt_file_kept_and_rebuilt(self):
        self.path.write_text("{坏的", encoding="utf-8")
        self.assertEqual(self.store.list(), [])
        job = self.store.create("后端", "甲", "")
        self.assertEqual(JobStore(self.path).list(), [job])
        backups = sorted(self.path.parent.glob("jobs.json.*.corrupt"))
        self.assertEqual([b.read_text(encoding="utf-8") for b in backups], ["{坏的"])

    def _backups(self):
        return sorted(self.path.parent.glob("jobs.json.*.corrupt"))

    def test_read_never_mutates_corrupt_file(self):
        self.path.write_text("{坏的", encoding="utf-8")
        self.assertEqual(self.store.list(), [])
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{坏的")
        self.assertEqual(self._backups(), [])

    def test_wrong_shape_backed_up_before_write(self):
        original = json.dumps({"jobs": [{"id": "x"}]}, ensure_ascii=False)
        self.path.write_text(original, encoding="utf-8")
        self.store.create("后端", "甲", "")
        self.assertEqual([b.read_text(encoding="utf-8") for b in self._backups()], [original])

    def test_rejected_entries_backed_up_before_write(self):
        good = {"id": "a" * 32, "type": "后端", "title": "好", "requirements": "",
                "created_at": "2026-11-09T10:00:00+08:00", "updated_at": "2026-11-09T10:00:00+08:00"}
        original = json.dumps([good, {"id": 1, "title": "被拒绝的原件"}], ensure_ascii=False)
        self.path.write_text(original, encoding="utf-8")
        self.store.update(good["id"], "前端", "改", "")
        self.assertEqual([b.read_text(encoding="utf-8") for b in self._backups()], [original])

    def test_clean_file_makes_no_backup(self):
        a = self.store.create("后端", "甲", "")
        self.store.update(a["id"], "后端", "乙", "")
        self.store.delete(a["id"])
        self.assertEqual(self._backups(), [])

    def test_two_corruptions_two_distinct_backups(self):
        self.path.write_text("{一", encoding="utf-8")
        self.store.create("后端", "甲", "")
        self.path.write_text("{二", encoding="utf-8")
        self.store.create("后端", "乙", "")
        self.assertEqual(sorted(b.read_text(encoding="utf-8") for b in self._backups()), ["{一", "{二"])

    def test_backup_failure_aborts_write_and_keeps_original(self):
        self.path.write_text("{坏的", encoding="utf-8")
        with mock.patch("common.shutil.copyfile", side_effect=OSError("磁盘满")):
            for op in (lambda: self.store.create("后端", "甲", ""),
                       lambda: self.store.delete("nope")):
                with self.assertRaises(common.DataProtectionError):
                    op()
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{坏的")
        self.assertEqual(self._backups(), [])

    def test_invalid_entries_skipped_on_load(self):
        good = {"id": "a" * 32, "type": "后端", "title": "好", "requirements": "",
                "created_at": "2026-11-09T10:00:00+08:00", "updated_at": "2026-11-09T10:00:00+08:00"}
        self.path.write_text(json.dumps([good, {"id": 1}, "x", dict(good, id="b" * 32, type="乱")]), encoding="utf-8")
        self.assertEqual(self.store.list(), [good])

    def test_job_limit(self):
        for i in range(interview_store.MAX_JOBS):
            self.store.create("后端", f"t{i}", "")
        with self.assertRaises(ValueError):
            self.store.create("后端", "多了", "")

    def test_no_resume_fields_persisted(self):
        self.store.create("后端", "甲", "要求")
        text = self.path.read_text(encoding="utf-8")
        self.assertNotIn("resume", text)


if __name__ == "__main__":
    unittest.main()
