"""web_server 的测试（FastAPI TestClient + 假录音/转写/时钟 + 临时目录；不联网、不碰麦克风和模型）。"""
import asyncio
import contextlib
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient

import meeting_store
import record
import voiceprint
import web_server
from meeting_controller import MeetingController
from tests.test_meeting_controller import FakeClock, FakeRecorder, FakeTranscriber
from voiceprint_service import VoiceprintService

WAIT = 5.0
PORT = 8765
ORIGIN = {"Origin": f"http://127.0.0.1:{PORT}"}
MID = "2026-11-09_1030"


def mr_threads():
    return [t for t in threading.enumerate() if t.name.startswith("mr-") and t.is_alive()]


def parse_sse(text: str) -> list:
    """把 SSE 文本解析成 [{id, event, data}]，忽略只有注释/retry 的块。"""
    out = []
    for block in text.split("\n\n"):
        ev = {}
        for line in block.split("\n"):
            if line.startswith("id: "):
                ev["id"] = int(line[4:])
            elif line.startswith("event: "):
                ev["event"] = line[7:]
            elif line.startswith("data: "):
                ev["data"] = json.loads(line[6:])
        if "event" in ev:
            out.append(ev)
    return out


class AppCase(unittest.TestCase):
    def setUp(self):
        self._baseline = set(mr_threads())
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self.assert_no_threads)
        self.root = self.tmp / "Meetings"
        self.root.mkdir()
        p = mock.patch.object(voiceprint, "PROFILE_DIR", self.tmp / "voiceprints")
        p.start()
        self.addCleanup(p.stop)
        self.rec = FakeRecorder(MID)
        self.tr = FakeTranscriber()
        self.clock = FakeClock()
        self.gate = threading.Event()
        self.vp_calls = []
        self.ctl = MeetingController(self.root, record_fn=self.rec, transcribe_fn=self.tr,
                                     clock=self.clock, start_timeout=WAIT)
        self.svc = VoiceprintService(record_enroll_fn=self.fake_enroll, import_fn=self.fake_import)
        self.addCleanup(self.ctl.close)
        self.addCleanup(self.svc.close)
        self.addCleanup(self.gate.set)
        self.config = self.tmp / "config.toml"      # 不存在 → 默认端口 8765
        self.app = web_server.create_app(self.ctl, root=self.root, config_path=self.config,
                                         voiceprint_service=self.svc)
        self.client = TestClient(self.app, base_url=f"http://127.0.0.1:{PORT}",
                                 raise_server_exceptions=False)

    def assert_no_threads(self):
        # 只检查本用例新增的线程（其他测试文件可能故意留下挂起的 mr-stream-* 线程）
        leaked = [t for t in mr_threads() if t not in self._baseline]
        for t in leaked:
            t.join(WAIT)
        self.assertEqual([t for t in leaked if t.is_alive()], [])

    # 假声纹登记：阻塞到 gate 或被取消
    def fake_enroll(self, name, seconds, replace, *, stop, on_progress):
        self.vp_calls.append(("record", name, seconds, replace))
        while not self.gate.is_set() and not stop.is_set():
            stop.wait(0.01)

    def fake_import(self, name, path, replace, *, stop):
        self.vp_calls.append(("import", name, str(path), replace))
        self.gate.wait(WAIT)

    # ---- 辅助 ----
    def mut(self, method, url, **kw):
        headers = dict(ORIGIN)
        headers.update(kw.pop("headers", {}))
        return self.client.request(method, url, headers=headers, **kw)

    def post(self, url, **kw):
        return self.mut("POST", url, **kw)

    def make_meeting(self, mid=MID, transcript=None, live=None):
        d = self.root / mid
        d.mkdir(parents=True)
        if transcript is not None:
            (d / "transcript.md").write_text(transcript, encoding="utf-8")
        if live is not None:
            (d / "live.txt").write_text(live, encoding="utf-8")
        return d

    def wait_final(self, want="completed"):
        for ev in self.ctl.subscribe(None, threading.Event(), WAIT):
            snap = self.ctl.snapshot()
            if ev is None or (snap["final"] == want and snap["state"] != "recording"):
                break
        self.assertEqual(self.ctl.snapshot()["final"], want)

    def record_and_finish(self):
        self.assertEqual(self.post("/api/recording/start").status_code, 200)
        self.assertEqual(self.post("/api/recording/stop").status_code, 200)
        self.wait_final()


class OriginGuardTests(AppCase):
    MUTATING = [
        ("POST", "/api/recording/start"), ("POST", "/api/recording/stop"),
        ("PATCH", f"/api/meetings/{MID}"), ("DELETE", f"/api/meetings/{MID}"),
        ("POST", f"/api/meetings/{MID}/annotations"),
        ("DELETE", "/api/voiceprints/x"), ("POST", "/api/voiceprints/jobs"),
        ("POST", "/api/voiceprints/jobs/abc/cancel"),
    ]

    def test_missing_origin_403_on_every_mutating_verb(self):
        self.make_meeting()
        for method, url in self.MUTATING:
            with self.subTest(method=method, url=url):
                r = self.client.request(method, url, json={})
                self.assertEqual(r.status_code, 403)
                self.assertIn("error", r.json())
        self.assertEqual(self.ctl.snapshot()["state"], "idle")
        self.assertTrue((self.root / MID).is_dir())

    def test_bad_origins_403(self):
        self.make_meeting()
        bad = ["http://evil.com:8765", "http://127.0.0.1.evil.com:8765", "http://localhost.evil.com:8765",
               "http://127.0.0.1:9999", "http://127.0.0.1", "http://localhost:9999", "null", "",
               "https://127.0.0.1:8765", "http://127.0.0.1:8765" + chr(64) + "evil.com", "http://[::1]:8765",
               "http://0.0.0.0:8765"]
        for origin in bad:
            for method, url in self.MUTATING[:4]:
                with self.subTest(origin=origin, method=method, url=url):
                    r = self.client.request(method, url, json={}, headers={"Origin": origin})
                    self.assertEqual(r.status_code, 403)
        self.assertEqual(self.ctl.snapshot()["state"], "idle")
        self.assertTrue((self.root / MID).is_dir())

    def test_localhost_origin_accepted(self):
        for origin in (f"http://localhost:{PORT}", f"http://127.0.0.1:{PORT}", f"http://LOCALHOST:{PORT}"):
            with self.subTest(origin=origin):
                r = self.client.post("/api/recording/stop", headers={"Origin": origin})
                self.assertEqual(r.status_code, 200)

    def test_origin_port_follows_explicit_port_kw(self):
        app = web_server.create_app(self.ctl, root=self.root, config_path=self.config,
                                    voiceprint_service=self.svc, port=9001)
        c = TestClient(app, base_url="http://127.0.0.1:9001", raise_server_exceptions=False)
        self.assertEqual(c.post("/api/recording/stop", headers={"Origin": "http://127.0.0.1:9001"}).status_code, 200)
        self.assertEqual(c.post("/api/recording/stop", headers={"Origin": ORIGIN["Origin"]}).status_code, 403)

    def test_port_from_config_file(self):
        self.config.write_text("[web]\nport = 9100\n", encoding="utf-8")
        app = web_server.create_app(self.ctl, root=self.root, config_path=self.config,
                                    voiceprint_service=self.svc)
        c = TestClient(app, base_url="http://127.0.0.1:9100", raise_server_exceptions=False)
        self.assertEqual(c.post("/api/recording/stop", headers={"Origin": "http://127.0.0.1:9100"}).status_code, 200)

    def test_get_needs_no_origin(self):
        self.assertEqual(self.client.get("/api/state").status_code, 200)

    def test_no_cors_headers(self):
        r = self.client.get("/api/state", headers={"Origin": "http://evil.com"})
        self.assertNotIn("access-control-allow-origin", r.headers)
        r = self.client.options("/api/recording/start", headers={
            "Origin": "http://evil.com", "Access-Control-Request-Method": "POST"})
        self.assertNotIn("access-control-allow-origin", r.headers)
        self.assertIn(r.status_code, (400, 403, 405))


class HostGuardTests(AppCase):
    def test_bad_host_rejected_everywhere(self):
        for host in ("evil.com", "evil.com:8765", "127.0.0.1.evil.com", "localhost.evil.com:8765",
                     "127.0.0.1:9999", "0.0.0.0:8765", "[::1]:8765", ""):
            for url in ("/", "/api/state", "/static/index.html"):
                with self.subTest(host=host, url=url):
                    r = self.client.get(url, headers={"Host": host})
                    self.assertIn(r.status_code, (400, 403))
                    self.assertIn("error", r.json())

    def test_good_hosts(self):
        for host in ("127.0.0.1:8765", "localhost:8765", "127.0.0.1", "localhost", "LocalHost:8765"):
            with self.subTest(host=host):
                self.assertEqual(self.client.get("/api/state", headers={"Host": host}).status_code, 200)


class BodyGuardTests(AppCase):
    def test_non_json_content_type_415(self):
        for ctype in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data", "application/jsonx"):
            with self.subTest(ctype=ctype):
                r = self.post("/api/recording/start", content=b'{"live": false}', headers={"content-type": ctype})
                self.assertEqual(r.status_code, 415)
                self.assertIn("error", r.json())
        r = self.post("/api/recording/start", content=b'{"live": false}', headers={"content-type": ""})
        self.assertEqual(r.status_code, 415)
        self.assertEqual(self.rec.calls, 0)

    def test_bodiless_post_needs_no_content_type(self):
        self.assertEqual(self.post("/api/recording/stop").status_code, 200)
        self.assertEqual(self.post("/api/recording/stop", headers={"content-type": "text/plain"}).status_code, 200)

    def test_json_with_charset_ok(self):
        r = self.post("/api/recording/start", content=b'{"speakers": 2}',
                      headers={"content-type": "application/json; charset=utf-8"})
        self.assertEqual(r.status_code, 200)

    def test_body_over_64kb_413(self):
        big = b'{"title": "' + b"a" * 70000 + b'"}'
        self.make_meeting()
        r = self.mut("PATCH", f"/api/meetings/{MID}", content=big, headers={"content-type": "application/json"})
        self.assertEqual(r.status_code, 413)
        self.assertIn("error", r.json())

    def test_chunked_body_over_limit_413(self):
        def gen():
            for _ in range(80):
                yield b"x" * 1024
        r = self.post("/api/recording/start", content=gen(), headers={"content-type": "application/json"})
        self.assertEqual(r.status_code, 413)
        self.assertEqual(self.rec.calls, 0)

    def test_body_at_limit_is_not_413(self):
        body = b'{"speakers": 2}' + b" " * (web_server.MAX_BODY_BYTES - 15)
        r = self.post("/api/recording/start", content=body, headers={"content-type": "application/json"})
        self.assertEqual(r.status_code, 200)

    def test_invalid_json_400(self):
        for body in (b"{bad", b"[1]", b'"x"', b"NaN", b'{"a": NaN}', b"\xff\xfe"):
            with self.subTest(body=body):
                r = self.post("/api/recording/start", content=body, headers={"content-type": "application/json"})
                self.assertEqual(r.status_code, 400)
                self.assertIn("error", r.json())
        self.assertEqual(self.rec.calls, 0)


class StaticTests(AppCase):
    def test_index_served_with_no_cache(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("text/html", r.headers["content-type"])
        self.assertIn("no-cache", r.headers["cache-control"])
        self.assertIn("id=\"tab-meeting\"", r.text)

    def test_static_no_cache(self):
        r = self.client.get("/static/index.html")
        self.assertEqual(r.status_code, 200)
        self.assertIn("no-cache", r.headers["cache-control"])

    def test_static_missing_and_traversal_404(self):
        for url in ("/static/nope.js", "/static/%2e%2e/web_server.py", "/static/..%2fweb_server.py",
                    "/static/%2e%2e%2f%2e%2e%2fREADME.md"):
            with self.subTest(url=url):
                r = self.client.get(url)
                self.assertEqual(r.status_code, 404)
                self.assertNotIn("create_app", r.text)

    def test_api_responses_not_cached(self):
        r = self.client.get("/api/state")
        self.assertIn("no-store", r.headers["cache-control"])


class ErrorTests(AppCase):
    def test_unknown_route_404_json(self):
        r = self.client.get("/api/nope")
        self.assertEqual(r.status_code, 404)
        self.assertIn("error", r.json())

    def test_wrong_method_405_json(self):
        r = self.client.get("/api/recording/start")
        self.assertEqual(r.status_code, 405)
        self.assertIn("error", r.json())

    def test_unhandled_exception_generic_500(self):
        with mock.patch.object(self.ctl, "snapshot", side_effect=RuntimeError("secret /Users/x/key")), \
                self.assertLogs("web_server", "ERROR"):
            r = self.client.get("/api/state")
        self.assertEqual(r.status_code, 500)
        self.assertIn("error", r.json())
        self.assertNotIn("secret", r.text)
        self.assertNotIn("/Users", r.text)
        self.assertNotIn("Traceback", r.text)

    def test_start_error_503_with_message(self):
        self.rec.fail_before_dir = None
        failing = mock.Mock(side_effect=record.RecorderError("打不开麦克风"))
        ctl = MeetingController(self.root, record_fn=lambda *a, **k: failing(), transcribe_fn=self.tr,
                                clock=self.clock, start_timeout=WAIT)
        self.addCleanup(ctl.close)
        app = web_server.create_app(ctl, root=self.root, config_path=self.config, voiceprint_service=self.svc)
        c = TestClient(app, base_url=f"http://127.0.0.1:{PORT}", raise_server_exceptions=False)
        r = c.post("/api/recording/start", headers=ORIGIN)
        self.assertEqual(r.status_code, 503)
        self.assertIn("打不开麦克风", r.json()["error"])


class RecordingRouteTests(AppCase):
    def test_state_has_snapshot_and_voiceprint_flag(self):
        s = self.client.get("/api/state").json()
        self.assertEqual(s["state"], "idle")
        self.assertEqual(s["final"], "idle")
        self.assertIs(s["voiceprint_busy"], False)

    def test_start_returns_meeting_id_and_stop_flow(self):
        r = self.post("/api/recording/start", json={"speakers": 3, "live": False})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["meeting_id"], MID)
        self.assertEqual(self.client.get("/api/state").json()["state"], "recording")
        self.assertEqual(self.post("/api/recording/stop").status_code, 200)
        self.wait_final()
        self.assertEqual(self.tr.calls, [(MID, 3)])
        self.assertEqual(self.client.get("/api/state").json()["final"], "completed")

    def test_start_twice_409(self):
        self.assertEqual(self.post("/api/recording/start").status_code, 200)
        r = self.post("/api/recording/start")
        self.assertEqual(r.status_code, 409)
        self.assertIn("error", r.json())

    def test_start_strict_types_400(self):
        for body in ({"speakers": True}, {"speakers": "3"}, {"speakers": 0}, {"speakers": 21},
                     {"speakers": 3.0}, {"live": "yes"}, {"live": 1}, {"unknown": 1}):
            with self.subTest(body=body):
                r = self.post("/api/recording/start", json=body)
                self.assertEqual(r.status_code, 400)
                self.assertIn("error", r.json())
        self.assertEqual(self.rec.calls, 0)

    def test_stop_when_idle_ok(self):
        r = self.post("/api/recording/stop")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["state"], "idle")

    def test_voiceprint_job_blocks_recording_409(self):
        r = self.post("/api/voiceprints/jobs", json={"kind": "record", "name": "张三", "seconds": 30})
        self.assertEqual(r.status_code, 202)
        self.assertIs(self.client.get("/api/state").json()["voiceprint_busy"], True)
        r = self.post("/api/recording/start")
        self.assertEqual(r.status_code, 409)

    def test_recording_blocks_voiceprint_job_409(self):
        self.assertEqual(self.post("/api/recording/start").status_code, 200)
        r = self.post("/api/voiceprints/jobs", json={"kind": "record", "name": "张三", "seconds": 30})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.vp_calls, [])


class MeetingRouteTests(AppCase):
    def test_list_and_detail(self):
        self.make_meeting("2026-11-08_0900")
        text = "# 会议\n- 说话人：张三、李四\n\n[00:00:01] 你好\n"
        self.make_meeting(MID, transcript=text, live="[00:00:01] 现场\n")
        lst = self.client.get("/api/meetings").json()["meetings"]
        self.assertEqual([m["id"] for m in lst], [MID, "2026-11-08_0900"])
        self.assertTrue(lst[0]["has_transcript"])
        d = self.client.get(f"/api/meetings/{MID}").json()
        self.assertEqual(d["id"], MID)
        self.assertEqual(d["title"], MID)
        self.assertEqual(d["transcript"], text)
        self.assertEqual(d["live"], "[00:00:01] 现场\n")
        self.assertEqual(d["speakers"], ["张三", "李四"])
        self.assertEqual(d["annotations"], [])
        self.assertIs(d["active"], False)
        d2 = self.client.get("/api/meetings/2026-11-08_0900").json()
        self.assertIsNone(d2["transcript"])
        self.assertIsNone(d2["live"])
        self.assertEqual(d2["speakers"], [])

    def test_rename_and_list_reflects(self):
        self.make_meeting()
        r = self.mut("PATCH", f"/api/meetings/{MID}", json={"title": "周会"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["title"], "周会")
        self.assertEqual(self.client.get("/api/meetings").json()["meetings"][0]["title"], "周会")

    def test_rename_validation_400(self):
        self.make_meeting()
        for body in ({"title": "a" * 101}, {"title": "a\nb"}, {"title": 5}, {"title": ""}, {}, {"title": "x", "z": 1}):
            with self.subTest(body=body):
                r = self.mut("PATCH", f"/api/meetings/{MID}", json=body)
                self.assertEqual(r.status_code, 400)
                self.assertIn("error", r.json())

    def test_delete(self):
        d = self.make_meeting()
        r = self.mut("DELETE", f"/api/meetings/{MID}")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(d.exists())
        self.assertEqual(self.mut("DELETE", f"/api/meetings/{MID}").status_code, 404)

    def test_delete_and_rename_during_recording_409(self):
        self.assertEqual(self.post("/api/recording/start").status_code, 200)
        r = self.mut("DELETE", f"/api/meetings/{MID}")
        self.assertEqual(r.status_code, 409)
        self.assertIn("error", r.json())
        self.assertEqual(self.mut("PATCH", f"/api/meetings/{MID}", json={"title": "x"}).status_code, 409)
        self.assertTrue((self.root / MID).is_dir())
        self.assertIs(self.client.get(f"/api/meetings/{MID}").json()["active"], True)

    def test_delete_and_rename_during_transcribing_409_then_ok(self):
        self.tr.gate.clear()
        self.addCleanup(self.tr.gate.set)
        self.assertEqual(self.post("/api/recording/start").status_code, 200)
        self.assertEqual(self.post("/api/recording/stop").status_code, 200)
        self.assertTrue(self.tr.entered.wait(WAIT))
        self.assertEqual(self.mut("DELETE", f"/api/meetings/{MID}").status_code, 409)
        self.assertEqual(self.mut("PATCH", f"/api/meetings/{MID}", json={"title": "x"}).status_code, 409)
        self.tr.gate.set()
        self.wait_final()
        self.assertEqual(self.mut("PATCH", f"/api/meetings/{MID}", json={"title": "x"}).status_code, 200)
        self.assertEqual(self.mut("DELETE", f"/api/meetings/{MID}").status_code, 200)

    def test_unknown_meeting_404_json(self):
        for method, url, kw in (("GET", "/api/meetings/nope", {}), ("PATCH", "/api/meetings/nope", {"json": {"title": "x"}}),
                                ("DELETE", "/api/meetings/nope", {}), ("GET", "/api/meetings/nope/annotations", {}),
                                ("POST", "/api/meetings/nope/annotations", {"json": {"kind": "note", "t": 1, "text": "x"}})):
            with self.subTest(method=method, url=url):
                r = self.mut(method, url, **kw)
                self.assertEqual(r.status_code, 404)
                self.assertIn("error", r.json())


class TraversalTests(AppCase):
    def setUp(self):
        super().setUp()
        self.secret_dir = self.tmp / "secret"
        self.secret_dir.mkdir()
        (self.secret_dir / "transcript.md").write_text("TOP-SECRET", encoding="utf-8")
        self.make_meeting()

    def ids(self):
        return ["..%2f", "..%2fsecret", "%2e%2e", "%2e%2e%2fsecret", "%2e%2e%2f%2e%2e%2fetc", "..%5csecret",
                "%2fetc", "%00", ".hidden", "%2e", "secret%2f..%2f..%2fsecret", "link"]

    def test_get_and_annotations_404(self):
        (self.root / "link").symlink_to(self.secret_dir)
        for mid in self.ids():
            for suffix in ("", "/annotations"):
                with self.subTest(mid=mid, suffix=suffix):
                    r = self.client.get(f"/api/meetings/{mid}{suffix}")
                    self.assertEqual(r.status_code, 404)
                    self.assertIn("error", r.json())
                    self.assertNotIn("TOP-SECRET", r.text)

    def test_mutations_404_and_nothing_touched(self):
        (self.root / "link").symlink_to(self.secret_dir)
        for mid in self.ids():
            with self.subTest(mid=mid):
                self.assertEqual(self.mut("DELETE", f"/api/meetings/{mid}").status_code, 404)
                self.assertEqual(self.mut("PATCH", f"/api/meetings/{mid}", json={"title": "x"}).status_code, 404)
                r = self.mut("POST", f"/api/meetings/{mid}/annotations", json={"kind": "note", "t": 1, "text": "x"})
                self.assertEqual(r.status_code, 404)
        self.assertEqual((self.secret_dir / "transcript.md").read_text(encoding="utf-8"), "TOP-SECRET")
        self.assertFalse((self.secret_dir / "annotations.json").exists())
        self.assertFalse((self.secret_dir / "meta.json").exists())
        self.assertTrue((self.root / MID).is_dir())


class AnnotationTests(AppCase):
    def test_add_and_list_keeps_decimals(self):
        self.make_meeting()
        r = self.mut("POST", f"/api/meetings/{MID}/annotations", json={"kind": "note", "t": 1.2345, "text": "重点"})
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["t"], 1.2345)
        lst = self.client.get(f"/api/meetings/{MID}/annotations").json()["annotations"]
        self.assertEqual(lst, [{"kind": "note", "t": 1.2345, "text": "重点"}])
        self.assertEqual(self.client.get(f"/api/meetings/{MID}").json()["annotations"], lst)

    def test_annotation_while_recording_allowed(self):
        self.assertEqual(self.post("/api/recording/start").status_code, 200)
        r = self.mut("POST", f"/api/meetings/{MID}/annotations", json={"kind": "highlight", "t": 3, "text": "a"})
        self.assertEqual(r.status_code, 201)

    def test_validation_400(self):
        self.make_meeting()
        bad = [{"kind": "x", "t": 1, "text": "a"}, {"kind": "note", "t": -1, "text": "a"},
               {"kind": "note", "t": True, "text": "a"}, {"kind": "note", "t": "1", "text": "a"},
               {"kind": "note", "t": 1, "text": ""}, {"kind": "note", "t": 1, "text": "a" * 2001},
               {"kind": "note", "t": 1}, {"t": 1, "text": "a"}, {"kind": "note", "t": 1, "text": 5},
               {"kind": "note", "t": 1, "text": "a", "extra": 1}]
        for body in bad:
            with self.subTest(body=str(body)[:60]):
                r = self.mut("POST", f"/api/meetings/{MID}/annotations", json=body)
                self.assertEqual(r.status_code, 400)
                self.assertIn("error", r.json())
        self.assertEqual(meeting_store.list_annotations(self.root / MID), [])


class VoiceprintRouteTests(AppCase):
    def save(self, name):
        voiceprint.save_profile(name, [1.0, 0.0, 0.0], seconds=12, chunks=4, model="m")

    def test_list_and_delete(self):
        self.save("张三")
        body = self.client.get("/api/voiceprints").json()
        self.assertEqual([v["name"] for v in body["voiceprints"]], ["张三"])
        self.assertNotIn("vector", body["voiceprints"][0])
        self.assertEqual(self.mut("DELETE", "/api/voiceprints/张三").status_code, 200)
        self.assertEqual(self.mut("DELETE", "/api/voiceprints/张三").status_code, 404)
        self.assertEqual(self.client.get("/api/voiceprints").json()["voiceprints"], [])

    def test_delete_bad_names_404(self):
        for name in ("..%2fx", "%2e%2e", "a.b", "说话人1"):
            with self.subTest(name=name):
                self.assertEqual(self.mut("DELETE", f"/api/voiceprints/{name}").status_code, 404)

    def test_record_job_lifecycle(self):
        r = self.post("/api/voiceprints/jobs", json={"kind": "record", "name": "张三", "seconds": 30, "replace": False})
        self.assertEqual(r.status_code, 202)
        jid = r.json()["job_id"]
        st = self.client.get(f"/api/voiceprints/jobs/{jid}").json()
        self.assertEqual(st["id"], jid)
        self.assertIn(st["state"], ("pending", "running"))
        c = self.post(f"/api/voiceprints/jobs/{jid}/cancel")
        self.assertEqual(c.status_code, 200)
        self.assertIs(c.json()["cancelled"], True)
        self.assertEqual(self.client.get(f"/api/voiceprints/jobs/{jid}").json()["state"], "cancelled")
        self.assertIs(self.post(f"/api/voiceprints/jobs/{jid}/cancel").json()["cancelled"], False)

    def test_second_job_409(self):
        body = {"kind": "record", "name": "张三", "seconds": 30}
        self.assertEqual(self.post("/api/voiceprints/jobs", json=body).status_code, 202)
        self.assertEqual(self.post("/api/voiceprints/jobs", json=body).status_code, 409)

    def test_import_job(self):
        f = self.tmp / "a.wav"
        f.write_bytes(b"x")
        r = self.post("/api/voiceprints/jobs", json={"kind": "import", "name": "李四", "path": str(f)})
        self.assertEqual(r.status_code, 202)
        self.gate.set()

    def test_job_validation_400(self):
        bad = [{}, {"kind": "zzz", "name": "a"}, {"kind": "record", "name": "a", "seconds": "30"},
               {"kind": "record", "name": "a", "seconds": 30.0}, {"kind": "record", "name": "a"},
               {"kind": "record", "name": "../x", "seconds": 30}, {"kind": "record", "name": 5, "seconds": 30},
               {"kind": "record", "name": "a", "seconds": 30, "replace": "yes"},
               {"kind": "import", "name": "a", "path": str(self.tmp / "nope.wav")},
               {"kind": "import", "name": "a"}, {"kind": "record", "name": "a", "seconds": 30, "x": 1}]
        for body in bad:
            with self.subTest(body=str(body)):
                r = self.post("/api/voiceprints/jobs", json=body)
                self.assertEqual(r.status_code, 400)
                self.assertIn("error", r.json())
        self.assertEqual(self.vp_calls, [])

    def test_unknown_job_404(self):
        self.assertEqual(self.client.get("/api/voiceprints/jobs/nope").status_code, 404)
        self.assertEqual(self.post("/api/voiceprints/jobs/nope/cancel").status_code, 404)


class SseHttpTests(AppCase):
    def stream(self, headers=None, url="/api/events"):
        """控制器已关闭时订阅会在发完积压事件后结束，所以读取必然有界。"""
        self.ctl.close()
        r = self.client.get(url, headers=headers or {})
        return r

    def test_headers_and_first_event_is_snapshot(self):
        r = self.stream()
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.headers["content-type"].startswith("text/event-stream"))
        self.assertIn("no-cache", r.headers["cache-control"])
        self.assertEqual(r.headers["x-accel-buffering"], "no")
        evs = parse_sse(r.text)
        self.assertEqual(evs[0]["event"], "state")
        self.assertEqual(evs[0]["id"], self.ctl.last_seq())
        self.assertEqual(evs[0]["data"]["state"], "idle")

    def test_resume_with_last_event_id_only_newer(self):
        self.record_and_finish()
        self.ctl.close()
        full = parse_sse(self.client.get("/api/events", headers={"Last-Event-ID": "0"}).text)
        ids = [e["id"] for e in full]
        self.assertGreaterEqual(len(ids), 4)
        self.assertEqual(ids, sorted(set(ids)))
        self.assertIn("final", [e["event"] for e in full])
        mid = ids[len(ids) // 2]
        part = parse_sse(self.client.get("/api/events", headers={"Last-Event-ID": str(mid)}).text)
        self.assertEqual([e["id"] for e in part], [i for i in ids if i > mid])
        none = parse_sse(self.client.get("/api/events", headers={"Last-Event-ID": str(ids[-1])}).text)
        self.assertEqual(none, [])

    def test_last_seq_query_and_header_precedence(self):
        self.record_and_finish()
        self.ctl.close()
        full = parse_sse(self.client.get("/api/events?last_seq=0").text)
        ids = [e["id"] for e in full]
        self.assertGreater(len(ids), 3)
        part = parse_sse(self.client.get(f"/api/events?last_seq={ids[1]}").text)
        self.assertEqual([e["id"] for e in part], ids[2:])
        both = parse_sse(self.client.get(f"/api/events?last_seq=0", headers={"Last-Event-ID": str(ids[1])}).text)
        self.assertEqual([e["id"] for e in both], ids[2:])

    def test_garbage_last_event_id_gives_snapshot(self):
        self.record_and_finish()
        self.ctl.close()
        for bad in ("abc", "-1", "1.5", ""):
            with self.subTest(bad=bad):
                evs = parse_sse(self.client.get("/api/events", headers={"Last-Event-ID": bad}).text)
                self.assertEqual(len(evs), 1)
                self.assertEqual(evs[0]["event"], "state")

    def test_stream_ends_when_controller_closes(self):
        box = {}

        def reader():
            c = TestClient(self.app, base_url=f"http://127.0.0.1:{PORT}", raise_server_exceptions=False)
            box["r"] = c.get("/api/events")
        t = threading.Thread(target=reader, name="test-sse-reader", daemon=True)
        t.start()
        t.join(0.2)
        self.ctl.close()
        t.join(WAIT)
        self.assertFalse(t.is_alive())
        self.assertEqual(parse_sse(box["r"].text)[0]["event"], "state")


class FakeController:
    """脚本化的控制器：subscribe 依次产出给定的事件（None 表示心跳节拍），然后结束。"""

    def __init__(self, script):
        self.script = list(script)
        self.stop_seen = None
        self.args = None

    def subscribe(self, last_seq=None, stop=None, timeout=15.0):
        self.args = (last_seq, timeout)
        self.stop_seen = stop
        for item in self.script:
            if stop is not None and stop.is_set():
                return
            yield item


def level(seq, rms=0.1):
    return {"seq": seq, "type": "level", "data": {"rms": rms}}


def state(seq):
    return {"seq": seq, "type": "state", "data": {"state": "recording"}}


async def collect(ctl, *, limit=100, **kw):
    out = []
    kw.setdefault("is_disconnected", _never)
    kw.setdefault("stop", threading.Event())
    async for chunk in web_server.event_stream(ctl, last_seq=kw.pop("last_seq", None), **kw):
        out.append(chunk.decode("utf-8"))
        if len(out) >= limit:
            break
    return out


async def _never():
    return False


class LevelThrottleTests(unittest.TestCase):
    def test_level_rate_limited_and_latest_kept(self):
        th = web_server.LevelThrottle(0.25)
        self.assertEqual([e["seq"] for e in th.offer(level(1), 0.0)], [1])
        self.assertEqual(th.offer(level(2), 0.05), [])
        self.assertEqual(th.offer(level(3), 0.10), [])
        self.assertEqual(th.flush(0.20), [])
        self.assertEqual([e["seq"] for e in th.flush(0.26)], [3])      # 只留最新一条
        self.assertEqual(th.flush(0.60), [])

    def test_max_four_per_second(self):
        th = web_server.LevelThrottle(0.25)
        sent = []
        for i in range(100):                    # 10 次/秒 × 10 秒
            sent += th.offer(level(i), i * 0.1)
            sent += th.flush(i * 0.1 + 0.05)
        self.assertLessEqual(len(sent), 4 * 10 + 1)
        self.assertGreater(len(sent), 10)

    def test_other_events_pass_and_drop_stale_pending_level(self):
        th = web_server.LevelThrottle(0.25)
        th.offer(level(1), 0.0)
        th.offer(level(2), 0.01)
        self.assertEqual([e["seq"] for e in th.offer(state(3), 0.02)], [3])
        self.assertEqual(th.flush(1.0), [])      # 过期电平不能晚于 state 发出（id 必须单调）


class EventStreamTests(unittest.TestCase):
    def test_format_and_retry_once(self):
        out = asyncio.run(collect(FakeController([state(5), state(6)])))
        self.assertEqual(out[0], 'retry: 2000\nid: 5\nevent: state\ndata: {"state": "recording"}\n\n')
        self.assertEqual(out[1], 'id: 6\nevent: state\ndata: {"state": "recording"}\n\n')

    def test_chinese_not_escaped(self):
        ev = {"seq": 1, "type": "warning", "data": {"message": "实时转写不可用"}}
        out = asyncio.run(collect(FakeController([ev])))
        self.assertIn("实时转写不可用", out[0])

    def test_heartbeat_after_interval(self):
        now = [0.0]

        def clock():
            return now[0]

        class Ctl(FakeController):
            def subscribe(self, last_seq=None, stop=None, timeout=15.0):
                for _ in range(3):
                    now[0] += 6.0
                    yield None
        out = asyncio.run(collect(Ctl([]), heartbeat_s=15.0, clock=clock))
        self.assertEqual(out, [": heartbeat\n\n"])      # 6、12 秒不发，18 秒发一次

    def test_levels_throttled_in_stream(self):
        now = [0.0]

        def clock():
            return now[0]

        class Ctl(FakeController):
            def subscribe(self, last_seq=None, stop=None, timeout=15.0):
                for i in range(1, 11):
                    now[0] += 0.05
                    yield level(i)
        out = asyncio.run(collect(Ctl([]), clock=clock, level_interval=0.25))
        self.assertLess(len(out), 5)
        self.assertTrue(out[0].startswith("retry"))

    def test_disconnect_ends_stream_and_sets_stop(self):
        stop = threading.Event()
        calls = [0]

        async def gone():
            calls[0] += 1
            return calls[0] > 2

        ctl = FakeController([state(i) for i in range(1, 50)])
        out = asyncio.run(collect(ctl, is_disconnected=gone, stop=stop))
        self.assertLess(len(out), 5)
        self.assertTrue(stop.is_set())

    def test_stop_event_ends_stream(self):
        stop = threading.Event()
        stop.set()
        out = asyncio.run(collect(FakeController([state(1)]), stop=stop))
        self.assertEqual(out, [])

    def test_passes_last_seq_and_tick_to_subscribe(self):
        ctl = FakeController([])
        asyncio.run(collect(ctl, last_seq=7, tick_s=0.1))
        self.assertEqual(ctl.args, (7, 0.1))


class LifespanTests(AppCase):
    def test_shutdown_closes_controller_and_service(self):
        with TestClient(self.app, base_url=f"http://127.0.0.1:{PORT}") as c:
            self.assertEqual(c.get("/api/state").status_code, 200)
            c.post("/api/recording/start", headers=ORIGIN)
            self.assertEqual(self.ctl.snapshot()["state"], "recording")
        self.assertEqual(self.ctl.snapshot()["state"], "idle")
        self.assertEqual(self.client.post("/api/recording/start", headers=ORIGIN).status_code, 409)

    def test_create_app_links_exclusion(self):
        self.assertTrue(self.svc.is_busy is not None)
        self.post("/api/voiceprints/jobs", json={"kind": "record", "name": "张三", "seconds": 30})
        from meeting_controller import MeetingBusy
        with self.assertRaises(MeetingBusy):
            self.ctl.start()


class WebEntryTests(unittest.TestCase):
    def test_import_has_no_side_effects(self):
        before = set(mr_threads())
        import web
        self.assertEqual(web.HOST, "127.0.0.1")
        self.assertEqual(web.CONFIG_PATH.name, "config.toml")
        self.assertEqual(set(mr_threads()), before)

    def test_build_app_creates_real_objects_without_starting(self):
        import web
        before = set(mr_threads())
        with tempfile.TemporaryDirectory() as tmp:
            app = web.build_app(config_path=Path(tmp) / "none.toml", root=Path(tmp) / "M")
            self.assertEqual(app.state.controller.snapshot()["state"], "idle")
            self.assertEqual(set(mr_threads()), before)
            c = TestClient(app, base_url=f"http://127.0.0.1:{PORT}")
            self.assertEqual(c.get("/api/state").status_code, 200)
            app.state.controller.close()

    def test_main_binds_loopback_only(self):
        import web
        with mock.patch.object(web.uvicorn, "run") as run, \
                mock.patch.object(web, "build_app", return_value="APP"), \
                mock.patch.object(web.web_config, "load_port", return_value=9123):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(web.main(), 0)
        run.assert_called_once()
        args, kw = run.call_args
        self.assertEqual(args, ("APP",))
        self.assertEqual(kw["host"], "127.0.0.1")
        self.assertEqual(kw["port"], 9123)
        self.assertEqual(kw["workers"], 1)
        self.assertIs(kw["reload"], False)


if __name__ == "__main__":
    unittest.main()
