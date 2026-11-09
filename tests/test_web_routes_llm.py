"""大模型相关路由的测试：假 opener 注入，不联网；复用 test_web_server.AppCase 的辅助方法。"""
import base64
import json
import unittest

from fastapi.testclient import TestClient

import web_server
from interview_store import JobStore
from tests.test_llm_client import FakeOpener, FakeResp, http_error, ok_body
from tests.test_web_server import MID, ORIGIN, PORT, AppCase

TRANSCRIPT = (
    "# 会议文字稿\n- 开始时间：2026-11-09 10:30\n- 时长：00:10:00\n- 说话人：说话人A、小王\n\n"
    "[00:00:01] 说话人A: 大家好\n[00:00:05] 小王: 你好\n[00:00:09] 说话人A: 开始吧\n")
ANON = "# 会议文字稿\n- 开始时间：2026-11-09 10:30\n- 时长：00:00:09\n\n[00:00:01] 第一句\n"
PNG = "data:image/png;base64," + base64.b64encode(b"\x89PNG fake").decode()


def data_uri(kind="png", size=10):
    return f"data:image/{kind};base64," + base64.b64encode(b"x" * size).decode()


class LlmCase(AppCase):
    def setUp(self):
        super().setUp()
        self.opener = FakeOpener()
        self.jobs_path = self.tmp / "jobs.json"
        self.store = JobStore(self.jobs_path)
        self.app = web_server.create_app(self.ctl, root=self.root, config_path=self.config,
                                         voiceprint_service=self.svc, llm_opener=self.opener,
                                         job_store=self.store)
        self.client = TestClient(self.app, base_url=f"http://127.0.0.1:{PORT}", raise_server_exceptions=False)

    def configure(self, **extra):
        lines = ['[llm]', 'base_url = "https://llm.example.com/v1"', 'api_key = "sk-secret-123"', 'model = "m-1"']
        lines += [f"{k} = {v}" for k, v in extra.items()]
        self.config.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def put(self, url, **kw):
        return self.mut("PUT", url, **kw)

    def delete(self, url, **kw):
        return self.mut("DELETE", url, **kw)

    def make_job(self, **kw):
        return self.store.create(kw.get("type", "后端"), kw.get("title", "Python 工程师"), kw.get("requirements", "熟悉 FastAPI"))


class SpeakersRouteTests(LlmCase):
    def test_speakers(self):
        self.make_meeting(transcript=TRANSCRIPT)
        r = self.client.get(f"/api/meetings/{MID}/speakers")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"speakers": ["说话人A", "小王"], "suspect_speakers": []})

    def test_speakers_anonymous_and_missing_transcript(self):
        self.make_meeting(transcript=ANON)
        self.assertEqual(self.client.get(f"/api/meetings/{MID}/speakers").json(),
                         {"speakers": [], "suspect_speakers": []})
        self.make_meeting("2026-11-09_1100")
        self.assertEqual(self.client.get("/api/meetings/2026-11-09_1100/speakers").json(),
                         {"speakers": [], "suspect_speakers": []})

    def test_unknown_meeting_404(self):
        self.assertEqual(self.client.get("/api/meetings/nope/speakers").status_code, 404)


class SummaryRouteTests(LlmCase):
    URL = f"/api/meetings/{MID}/summary"

    def test_unconfigured_503(self):
        self.make_meeting(transcript=TRANSCRIPT)
        r = self.post(self.URL)
        self.assertEqual(r.status_code, 503)
        self.assertIn("error", r.json())
        self.assertEqual(self.opener.requests, [])

    def test_summary_ok_all_speakers(self):
        self.configure()
        self.make_meeting(transcript=TRANSCRIPT)
        self.opener.results.append(FakeResp(ok_body("纪要正文")))
        r = self.post(self.URL)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"summary": "纪要正文"})
        body = json.loads(self.opener.requests[0].data.decode("utf-8"))
        user = body["messages"][1]["content"]
        self.assertIn("大家好", user)
        self.assertIn("你好", user)
        self.assertEqual(self.opener.requests[0].get_header("Authorization"), "Bearer sk-secret-123")

    def test_summary_selected_speakers(self):
        self.configure()
        self.make_meeting(transcript=TRANSCRIPT)
        self.opener.results.append(FakeResp(ok_body("x")))
        r = self.post(self.URL, json={"speakers": ["小王"]})
        self.assertEqual(r.status_code, 200)
        user = json.loads(self.opener.requests[0].data.decode("utf-8"))["messages"][1]["content"]
        self.assertNotIn("大家好", user)
        self.assertIn("你好", user)

    def test_bad_speakers_400(self):
        self.configure()
        self.make_meeting(transcript=TRANSCRIPT)
        for body in ({"speakers": ["路人"]}, {"speakers": []}, {"speakers": "小王"}, {"speakers": [1]},
                     {"foo": 1}):
            with self.subTest(body=body):
                self.assertEqual(self.post(self.URL, json=body).status_code, 400)
        self.assertEqual(self.opener.requests, [])

    def test_too_long_413(self):
        self.configure(max_input_chars=5)
        self.make_meeting(transcript=TRANSCRIPT)
        r = self.post(self.URL)
        self.assertEqual(r.status_code, 413)
        self.assertIn("error", r.json())
        self.assertEqual(self.opener.requests, [])

    def test_selected_speaker_under_limit_ok(self):
        self.configure(max_input_chars=2)               # 小王只有 2 字
        self.make_meeting(transcript=TRANSCRIPT)
        self.opener.results.append(FakeResp(ok_body("x")))
        self.assertEqual(self.post(self.URL, json={"speakers": ["小王"]}).status_code, 200)

    def test_no_transcript_or_no_speech(self):
        self.configure()
        self.make_meeting()
        self.assertEqual(self.post(self.URL).status_code, 404)
        self.make_meeting("2026-11-09_1100", transcript="# 会议文字稿\n\n（未识别到语音）\n")
        self.assertEqual(self.post("/api/meetings/2026-11-09_1100/summary").status_code, 400)

    def test_unknown_meeting_404(self):
        self.configure()
        self.assertEqual(self.post("/api/meetings/nope/summary").status_code, 404)

    def test_llm_errors_502_without_secrets(self):
        self.configure()
        self.make_meeting(transcript=TRANSCRIPT)
        self.opener.results.append(http_error(500, b"sk-secret-123 boom"))
        r = self.post(self.URL)
        self.assertEqual(r.status_code, 502)
        self.assertNotIn("sk-secret-123", r.text)
        self.assertNotIn("boom", r.text)
        self.opener.results.append(TimeoutError())
        r = self.post(self.URL)
        self.assertEqual(r.status_code, 502)
        self.assertIn("超时", r.json()["error"])

    def test_guards(self):
        self.configure()
        self.make_meeting(transcript=TRANSCRIPT)
        self.assertEqual(self.client.post(self.URL, json={}).status_code, 403)
        r = self.client.post(self.URL, content=b'{"speakers": []}', headers=dict(ORIGIN, **{"Content-Type": "text/plain"}))
        self.assertEqual(r.status_code, 415)
        self.assertEqual(self.opener.requests, [])


class JobRouteTests(LlmCase):
    def test_crud(self):
        self.assertEqual(self.client.get("/api/jobs").json(), {"jobs": []})
        r = self.post("/api/jobs", json={"type": "后端", "title": "Python", "requirements": "FastAPI"})
        self.assertEqual(r.status_code, 201)
        job = r.json()
        self.assertEqual((job["type"], job["title"], job["requirements"]), ("后端", "Python", "FastAPI"))
        self.assertEqual(self.client.get("/api/jobs").json(), {"jobs": [job]})
        r = self.put(f"/api/jobs/{job['id']}", json={"type": "前端", "title": "JS", "requirements": "Vue"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["type"], "前端")
        self.assertEqual(r.json()["id"], job["id"])
        self.assertEqual(JobStore(self.jobs_path).list()[0]["title"], "JS")
        self.assertEqual(self.delete(f"/api/jobs/{job['id']}").json(), {"ok": True})
        self.assertEqual(self.client.get("/api/jobs").json(), {"jobs": []})

    def test_validation_400(self):
        bad = [{"type": "管理", "title": "a"}, {"type": "后端", "title": "x" * 101},
               {"type": "后端", "title": "a\nb"}, {"type": "后端", "title": "a", "requirements": "y" * 10001},
               {"title": "a"}, {"type": "后端"}, {"type": "后端", "title": "a", "extra": 1}]
        for body in bad:
            with self.subTest(body=str(body)[:30]):
                r = self.post("/api/jobs", json=body)
                self.assertEqual(r.status_code, 400)
                self.assertIn("error", r.json())
        self.assertEqual(self.store.list(), [])

    def test_unknown_job_404(self):
        self.assertEqual(self.put("/api/jobs/nope", json={"type": "后端", "title": "a"}).status_code, 404)
        self.assertEqual(self.delete("/api/jobs/nope").status_code, 404)

    def test_guards(self):
        job = self.make_job()
        for method, url in (("POST", "/api/jobs"), ("PUT", f"/api/jobs/{job['id']}"),
                            ("DELETE", f"/api/jobs/{job['id']}"), ("POST", f"/api/jobs/{job['id']}/questions")):
            with self.subTest(method=method, url=url):
                self.assertEqual(self.client.request(method, url, json={}).status_code, 403)
                r = self.client.request(method, url, content=b"{}", headers=dict(ORIGIN, **{"Content-Type": "text/plain"}))
                self.assertEqual(r.status_code, 415)
        self.assertEqual(len(self.store.list()), 1)

    def test_default_store_beside_config(self):
        app = web_server.create_app(self.ctl, root=self.root, config_path=self.config, voiceprint_service=self.svc)
        c = TestClient(app, base_url=f"http://127.0.0.1:{PORT}", raise_server_exceptions=False)
        self.assertEqual(c.post("/api/jobs", json={"type": "测试", "title": "t"}, headers=ORIGIN).status_code, 201)
        self.assertTrue((self.tmp / "jobs.json").is_file())
        self.assertFalse((self.root / "jobs.json").exists())


class QuestionsRouteTests(LlmCase):
    def url(self, job=None):
        return f"/api/jobs/{(job or self.job)['id']}/questions"

    def setUp(self):
        super().setUp()
        self.job = self.make_job()

    def test_unconfigured_503(self):
        r = self.post(self.url(), json={"resume_text": "简历"})
        self.assertEqual(r.status_code, 503)
        self.assertEqual(self.opener.requests, [])

    def test_unknown_job_404(self):
        self.configure()
        self.assertEqual(self.post("/api/jobs/nope/questions", json={}).status_code, 404)

    def test_text_resume_ok_and_nothing_persisted(self):
        self.configure()
        self.opener.results.append(FakeResp(ok_body("1. 题目\n**答案要点：**\n- 点")))
        r = self.post(self.url(), json={"resume_text": "我是张三独有简历"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"questions": "1. 题目\n**答案要点：**\n- 点"})
        body = json.loads(self.opener.requests[0].data.decode("utf-8"))
        self.assertEqual(body["messages"][0]["role"], "system")
        self.assertIn("专业能力", body["messages"][0]["content"])
        self.assertIn("我是张三独有简历", body["messages"][1]["content"])
        self.assertIn("熟悉 FastAPI", body["messages"][1]["content"])
        self.assertNotIn("张三", self.jobs_path.read_text(encoding="utf-8"))
        self.assertNotIn("题目", self.jobs_path.read_text(encoding="utf-8"))

    def test_empty_body_ok(self):
        self.configure()
        self.opener.results.append(FakeResp(ok_body("q")))
        self.assertEqual(self.post(self.url()).status_code, 200)

    def test_images_two_calls(self):
        self.configure()
        self.opener.results += [FakeResp(ok_body("图里的简历文字")), FakeResp(ok_body("题目们"))]
        r = self.post(self.url(), json={"resume_text": "补充", "images": [PNG, data_uri("jpeg"), data_uri("webp"), data_uri("gif")]})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json(), {"questions": "题目们"})
        first = json.loads(self.opener.requests[0].data.decode("utf-8"))
        self.assertEqual(len([p for p in first["messages"][-1]["content"] if p["type"] == "image_url"]), 4)
        second = json.loads(self.opener.requests[1].data.decode("utf-8"))
        self.assertIn("图里的简历文字", second["messages"][1]["content"])
        self.assertIn("补充", second["messages"][1]["content"])

    def test_image_validation_422(self):
        self.configure()
        bad = [
            [PNG] * 6,
            ["data:image/svg+xml;base64,AAAA"], ["data:text/plain;base64,AAAA"], ["https://x.com/a.png"],
            ["data:image/png;base64,"], ["data:image/png;base64,!!!notbase64"], ["data:image/png,AAAA"],
            [5], "notalist", [data_uri("png", 5 * 1024 * 1024 + 1)],
        ]
        for images in bad:
            with self.subTest(images=str(images)[:40]):
                r = self.post(self.url(), json={"images": images})
                self.assertEqual(r.status_code, 422)
                self.assertIn("error", r.json())
        self.assertEqual(self.opener.requests, [])

    def test_image_exactly_5mb_ok(self):
        self.configure()
        self.opener.results += [FakeResp(ok_body("t")), FakeResp(ok_body("q"))]
        r = self.post(self.url(), json={"images": [data_uri("png", 5 * 1024 * 1024)]})
        self.assertEqual(r.status_code, 200)

    def test_resume_length_422(self):
        self.configure()
        self.assertEqual(self.post(self.url(), json={"resume_text": "字" * 20001}).status_code, 422)
        self.assertEqual(self.post(self.url(), json={"resume_text": 5}).status_code, 422)
        self.opener.results.append(FakeResp(ok_body("q")))
        self.assertEqual(self.post(self.url(), json={"resume_text": "字" * 20000}).status_code, 200)

    def test_other_body_limits_still_apply(self):
        r = self.post("/api/jobs", content=b"x" * (70 * 1024), headers={"Content-Type": "application/json"})
        self.assertEqual(r.status_code, 413)

    def test_unknown_keys_400(self):
        self.configure()
        self.assertEqual(self.post(self.url(), json={"foo": 1}).status_code, 400)

    def test_llm_error_502(self):
        self.configure()
        self.opener.results.append(http_error(401, b"sk-secret-123"))
        r = self.post(self.url(), json={})
        self.assertEqual(r.status_code, 502)
        self.assertNotIn("sk-secret-123", r.text)
        self.opener.results += [FakeResp(ok_body("t")), http_error(429)]
        self.assertEqual(self.post(self.url(), json={"images": [PNG]}).status_code, 502)


if __name__ == "__main__":
    unittest.main()
