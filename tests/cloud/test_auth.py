"""Task 4 authentication, tenancy and privacy contracts.

The suite runs against a real PostgreSQL database. Identity uses the
explicitly test-only fake provider because gate G1 is unanswered: production
startup refuses that provider (see ConfigCase).
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from pydantic import ValidationError

from cloud.app import create_app
from cloud.settings import IMAGE_MAX_BYTES, Settings
from tests.cloud.support import A_EMAIL, B_EMAIL, CloudCase


class AuthCase(CloudCase):
    def test_own_meetings_only(self):
        mid = self.create_meeting(self.a)

        self.assertEqual(self.a.get(f"/api/cloud/meetings/{mid}").status_code, 200)
        self.assertEqual(self.b.get(f"/api/cloud/meetings/{mid}").status_code, 404)
        self.assertEqual(self.anon.get(f"/api/cloud/meetings/{mid}").status_code, 401)

        page = self.b.get("/api/cloud/meetings").json()
        self.assertNotIn(mid, [item["id"] for item in page["items"]])
        self.assertEqual(page["items"], [])

        own_page = self.a.get("/api/cloud/meetings").json()
        self.assertEqual([item["id"] for item in own_page["items"]], [mid])

    def test_owner_injection_rejected(self):
        response = self.a.post(
            "/api/cloud/meetings",
            json={"title": "Injected", "owner_user_id": self.b_user_id},
            headers=self.csrf_headers(self.a),
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.query_count("meetings"), 0)

        response = self.a.post(
            "/api/cloud/meetings",
            json={"title": "Injected", "ownerUserId": self.b_user_id},
            headers=self.csrf_headers(self.a),
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.query_count("meetings"), 0)

        mid = self.create_meeting(self.a)
        response = self.a.patch(
            f"/api/cloud/meetings/{mid}",
            json={"title": "Renamed", "owner_user_id": self.b_user_id},
            headers=self.csrf_headers(self.a),
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.a.get(f"/api/cloud/meetings/{mid}").json()["title"], "Weekly sync")

    def test_forged_session_and_csrf_rejected(self):
        mid = self.create_meeting(self.a)
        forged = "cloud_session=" + "0" * 43

        self.assertEqual(
            self.anon.get("/api/cloud/meetings", headers={"cookie": forged}).status_code, 401
        )
        self.assertEqual(
            self.anon.get(f"/api/cloud/meetings/{mid}", headers={"cookie": forged}).status_code,
            401,
        )
        self.assertEqual(
            self.anon.get("/api/cloud/auth/session", headers={"cookie": forged}).status_code, 401
        )
        self.assertEqual(
            self.anon.post("/api/cloud/meetings", json={"title": "anonymous"}).status_code, 401
        )

        self.assertEqual(
            self.a.post("/api/cloud/meetings", json={"title": "no csrf"}).status_code, 403
        )
        self.assertEqual(
            self.a.patch(f"/api/cloud/meetings/{mid}", json={"title": "no csrf"}).status_code, 403
        )
        response = self.a.patch(
            f"/api/cloud/meetings/{mid}",
            json={"title": "wrong token"},
            headers={"X-CSRF-Token": "not-the-token"},
        )
        self.assertEqual(response.status_code, 403)

        response = self.a.patch(
            f"/api/cloud/meetings/{mid}",
            json={"title": "cross origin"},
            headers={**self.csrf_headers(self.a), "Origin": "http://evil.example"},
        )
        self.assertEqual(response.status_code, 403)

        self.assertEqual(self.query_count("meetings"), 1)
        self.assertEqual(self.a.get(f"/api/cloud/meetings/{mid}").json()["title"], "Weekly sync")

        missing = "00000000-0000-4000-8000-000000000000"
        unknown = self.a.get(f"/api/cloud/meetings/{missing}")
        other = self.b.get(f"/api/cloud/meetings/{mid}")
        self.assertEqual(unknown.status_code, 404)
        self.assertEqual(other.status_code, 404)
        # The machine code is identical; only the per-request trace id differs.
        self.assertEqual(unknown.json()["error"], other.json()["error"])
        self.assertRegex(unknown.json()["request_id"], r"^req_[0-9a-f]{32}$")
        self.assertRegex(other.json()["request_id"], r"^req_[0-9a-f]{32}$")
        self.assertNotEqual(unknown.json()["request_id"], other.json()["request_id"])

    def test_invalid_speakers(self):
        for value in (0, 21, True, False, "2", 1.5):
            response = self.a.post(
                "/api/cloud/meetings",
                json={"title": "Speakers", "speakers": value},
                headers=self.csrf_headers(self.a),
            )
            self.assertEqual(
                response.status_code, 422, msg=f"speakers={value!r} must be rejected"
            )
        self.assertEqual(self.query_count("meetings"), 0)

        for value in (None, 1, 20):
            response = self.a.post(
                "/api/cloud/meetings",
                json={"title": f"ok-{value}", "speakers": value},
                headers=self.csrf_headers(self.a),
            )
            self.assertEqual(
                response.status_code, 201, msg=f"speakers={value!r} must be accepted"
            )
        self.assertEqual(self.query_count("meetings"), 3)

        for payload in (
            {"title": "mode", "mode": "stereo"},
            {"title": "   "},
            {"title": ""},
        ):
            response = self.a.post(
                "/api/cloud/meetings", json=payload, headers=self.csrf_headers(self.a)
            )
            self.assertEqual(response.status_code, 422, msg=f"payload={payload!r}")

    def test_private_pagination_and_rename(self):
        a_ids = [self.create_meeting(self.a, title=f"A {index}") for index in range(21)]
        b_id = self.create_meeting(self.b, title="B only")

        first = self.a.get("/api/cloud/meetings").json()
        self.assertEqual(len(first["items"]), 20)
        self.assertIsNotNone(first["next_cursor"])
        second = self.a.get(
            "/api/cloud/meetings", params={"cursor": first["next_cursor"]}
        ).json()
        self.assertEqual(len(second["items"]), 1)
        self.assertIsNone(second["next_cursor"])

        collected = [item["id"] for item in first["items"] + second["items"]]
        self.assertEqual(len(collected), 21)
        self.assertEqual(len(set(collected)), 21)
        self.assertEqual(collected, sorted(a_ids, reverse=True))
        self.assertNotIn(b_id, collected)

        b_page = self.b.get("/api/cloud/meetings").json()
        self.assertEqual([item["id"] for item in b_page["items"]], [b_id])

        page1 = self.a.get("/api/cloud/meetings", params={"limit": 10}).json()
        page1_ids = [item["id"] for item in page1["items"]]
        self.clock.advance(seconds=1)
        later = self.create_meeting(self.a, title="later")
        page2 = self.a.get(
            "/api/cloud/meetings",
            params={"limit": 10, "cursor": page1["next_cursor"]},
        ).json()
        page2_ids = [item["id"] for item in page2["items"]]
        self.assertNotIn(later, page2_ids)
        self.assertFalse(set(page2_ids) & set(page1_ids))

        b_with_cursor = self.b.get(
            "/api/cloud/meetings",
            params={"limit": 10, "cursor": page1["next_cursor"]},
        ).json()
        for item in b_with_cursor["items"]:
            self.assertEqual(self.b.get(f"/api/cloud/meetings/{item['id']}").status_code, 200)

        for bad in (0, 101, -1):
            self.assertEqual(
                self.a.get("/api/cloud/meetings", params={"limit": bad}).status_code, 422
            )
        self.assertEqual(
            self.a.get("/api/cloud/meetings", params={"limit": 1}).status_code, 200
        )
        self.assertEqual(
            self.a.get("/api/cloud/meetings", params={"limit": 100}).status_code, 200
        )
        self.assertEqual(
            self.a.get("/api/cloud/meetings", params={"cursor": "garbage"}).status_code, 400
        )
        self.assertEqual(
            self.a.get(
                "/api/cloud/meetings", params={"cursor": page1["next_cursor"] + "x"}
            ).status_code,
            400,
        )

        target = a_ids[0]
        response = self.b.patch(
            f"/api/cloud/meetings/{target}",
            json={"title": "hijacked"},
            headers=self.csrf_headers(self.b),
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.a.get(f"/api/cloud/meetings/{target}").json()["title"], "A 0")

        renamed = self.a.patch(
            f"/api/cloud/meetings/{target}",
            json={"title": "Renamed"},
            headers=self.csrf_headers(self.a),
        )
        self.assertEqual(renamed.status_code, 200)
        self.assertEqual(renamed.json()["title"], "Renamed")
        self.assertEqual(self.b.get(f"/api/cloud/meetings/{target}").status_code, 404)


class LoginCase(CloudCase):
    def test_login_callback_replay_rejected(self):
        sessions_before = self.query_count("sessions")
        client = TestClient(self.app, follow_redirects=False)
        start = client.get("/api/cloud/auth/login", params={"email": A_EMAIL})
        self.assertEqual(start.status_code, 303)
        pending_cookie = client.cookies.get("cloud_session")
        self.assertTrue(pending_cookie)

        authorize = client.get(start.headers["location"])
        self.assertEqual(authorize.status_code, 303)
        callback_url = authorize.headers["location"]

        finished = client.get(callback_url)
        self.assertEqual(finished.status_code, 200)
        self.assertIn("csrf_token", finished.json())
        sessions_after_login = self.query_count("sessions")
        # exactly two new rows: the consumed pending login and the authenticated session
        self.assertEqual(sessions_after_login, sessions_before + 2)

        replay = TestClient(self.app, follow_redirects=False)
        replay.cookies.set("cloud_session", pending_cookie)
        self.assertEqual(replay.get(callback_url).status_code, 400)
        self.assertEqual(self.query_count("sessions"), sessions_after_login)

        reused = TestClient(self.app, follow_redirects=False)
        reused.cookies.set("cloud_session", client.cookies.get("cloud_session"))
        self.assertEqual(reused.get(callback_url).status_code, 400)
        self.assertEqual(self.query_count("sessions"), sessions_after_login)
        self.assertEqual(client.get("/api/cloud/auth/session").status_code, 200)

        other = TestClient(self.app, follow_redirects=False)
        other_start = other.get("/api/cloud/auth/login", params={"email": A_EMAIL})
        self.assertEqual(other_start.status_code, 303)
        other_authorize = other.get(other_start.headers["location"])
        self.assertEqual(
            other.get(
                "/api/cloud/auth/callback", params={"code": "x", "state": "wrong"}
            ).status_code,
            400,
        )
        self.assertEqual(other_authorize.status_code, 303)

        expiring = TestClient(self.app, follow_redirects=False)
        late_start = expiring.get("/api/cloud/auth/login", params={"email": A_EMAIL})
        late_authorize = expiring.get(late_start.headers["location"])
        self.assertEqual(late_authorize.status_code, 303)
        self.clock.advance(seconds=self.settings.login_state_ttl_s + 1)
        self.assertEqual(expiring.get(late_authorize.headers["location"]).status_code, 400)

    def test_login_failure_is_generic(self):
        client = TestClient(self.app, follow_redirects=False)
        start = client.get("/api/cloud/auth/login", params={"email": A_EMAIL})
        authorize = client.get(start.headers["location"])
        callback_url = authorize.headers["location"]
        tampered = callback_url.replace("code=", "code=x", 1)
        response = client.get(tampered)
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"], "login_failed")
        self.assertRegex(response.json()["request_id"], r"^req_[0-9a-f]{32}$")
        self.assertNotIn("alice", response.text)
        self.assertNotIn("example.test", response.text)

    def test_login_next_redirects_to_original_page(self):
        client = TestClient(self.app, follow_redirects=False)
        start = client.get(
            "/api/cloud/auth/login",
            params={"email": A_EMAIL, "next": "/voiceprints.html"},
        )
        self.assertEqual(start.status_code, 303)
        start_cookies = start.headers.get_list("set-cookie")
        next_header = next(h for h in start_cookies if h.startswith("cloud_login_next="))
        self.assertIn("HttpOnly", next_header)
        self.assertIn("SameSite=lax", next_header)

        authorize = client.get(start.headers["location"])
        self.assertEqual(authorize.status_code, 303)
        finished = client.get(authorize.headers["location"])
        self.assertEqual(finished.status_code, 303)
        self.assertTrue(finished.headers["location"].endswith("/voiceprints.html"))

        headers = finished.headers.get_list("set-cookie")
        self.assertTrue(any(h.startswith("cloud_session=") for h in headers))
        self.assertTrue(any(h.startswith("cloud_csrf=") for h in headers))
        self.assertTrue(
            any(
                h.startswith("cloud_login_next=") and "Max-Age=0" in h
                for h in headers
            )
        )
        self.assertEqual(client.get("/api/cloud/auth/session").status_code, 200)

    def test_login_next_rejects_offsite_or_unsafe_targets(self):
        unsafe = (
            "//evil.example.com/a",
            "/a\\b",
            "https://evil.example.com/a",
            "/a\x00b",
            "/" + "a" * 512,
            "",
        )
        for next_value in unsafe:
            with self.subTest(next=next_value):
                client = TestClient(self.app, follow_redirects=False)
                start = client.get(
                    "/api/cloud/auth/login",
                    params={"email": A_EMAIL, "next": next_value},
                )
                self.assertEqual(start.status_code, 303)
                self.assertFalse(
                    any(
                        h.startswith("cloud_login_next=")
                        for h in start.headers.get_list("set-cookie")
                    )
                )
                authorize = client.get(start.headers["location"])
                self.assertEqual(authorize.status_code, 303)
                finished = client.get(authorize.headers["location"])
                self.assertEqual(finished.status_code, 200)
                self.assertIn("csrf_token", finished.json())

    def test_login_without_next_keeps_json_response(self):
        client = TestClient(self.app, follow_redirects=False)
        start = client.get("/api/cloud/auth/login", params={"email": A_EMAIL})
        self.assertEqual(start.status_code, 303)
        self.assertFalse(
            any(
                h.startswith("cloud_login_next=")
                for h in start.headers.get_list("set-cookie")
            )
        )
        authorize = client.get(start.headers["location"])
        self.assertEqual(authorize.status_code, 303)
        finished = client.get(authorize.headers["location"])
        self.assertEqual(finished.status_code, 200)
        self.assertIn("csrf_token", finished.json())

    def test_session_cookie_flags_and_logout(self):
        client = TestClient(self.app, follow_redirects=False)
        start = client.get("/api/cloud/auth/login", params={"email": A_EMAIL})
        start_cookie = start.headers.get("set-cookie", "")
        self.assertIn("cloud_session=", start_cookie)
        self.assertIn("HttpOnly", start_cookie)
        self.assertIn("SameSite=lax", start_cookie)
        self.assertNotIn("Secure", start_cookie)

        authorize = client.get(start.headers["location"])
        finished = client.get(authorize.headers["location"])
        self.assertEqual(finished.status_code, 200)
        headers = finished.headers.get_list("set-cookie")
        session_header = next(h for h in headers if h.startswith("cloud_session="))
        csrf_header = next(h for h in headers if h.startswith("cloud_csrf="))
        self.assertIn("HttpOnly", session_header)
        self.assertIn("SameSite=lax", session_header)
        self.assertNotIn("HttpOnly", csrf_header)

        secure_settings = self.settings.model_copy(update={"cookie_secure": True})
        secure_app = create_app(secure_settings, {"engine": self.engine, "clock": self.clock})
        secure_client = TestClient(
            secure_app, base_url="https://testserver", follow_redirects=False
        )
        secure_start = secure_client.get("/api/cloud/auth/login", params={"email": A_EMAIL})
        self.assertIn("Secure", secure_start.headers.get("set-cookie", ""))
        secure_authorize = secure_client.get(secure_start.headers["location"])
        secure_done = secure_client.get(secure_authorize.headers["location"])
        self.assertEqual(secure_done.status_code, 200, secure_done.text)
        self.assertIn("Secure", secure_done.headers.get("set-cookie", ""))

        stolen = self.a.cookies.get("cloud_session")
        self.assertEqual(self.a.post("/api/cloud/auth/logout").status_code, 403)
        logout = self.a.post(
            "/api/cloud/auth/logout", headers=self.csrf_headers(self.a)
        )
        self.assertEqual(logout.status_code, 204)
        cleared = logout.headers.get_list("set-cookie")
        self.assertTrue(
            any(h.startswith("cloud_session=") and "Max-Age=0" in h for h in cleared)
        )
        self.assertTrue(
            any(h.startswith("cloud_csrf=") and "Max-Age=0" in h for h in cleared)
        )

        replay = TestClient(self.app)
        replay.cookies.set("cloud_session", stolen)
        self.assertEqual(replay.get("/api/cloud/meetings").status_code, 401)
        self.assertEqual(self.b.get("/api/cloud/meetings").status_code, 200)

    def test_session_survives_app_restart(self):
        mid = self.create_meeting(self.a)
        self.restart_app()
        self.assertEqual(self.a.get(f"/api/cloud/meetings/{mid}").status_code, 200)
        self.assertEqual(self.b.get(f"/api/cloud/meetings/{mid}").status_code, 404)

        self.clock.advance(seconds=self.settings.session_ttl_s + 1)
        self.assertEqual(self.a.get("/api/cloud/meetings").status_code, 401)


class ConfigCase(CloudCase):
    def test_production_refuses_fake_auth(self):
        base = {
            "database_url": self.database_url,
            "secret_key": "x" * 40,
            "environment": "production",
            "public_base_url": "https://cloud.example.test",
        }
        with self.assertRaises(RuntimeError) as raised:
            create_app(Settings(**base, auth_provider="fake_test", cookie_secure=True))
        self.assertIn("fake", str(raised.exception).lower())

        with self.assertRaises(RuntimeError) as raised:
            create_app(Settings(**base, auth_provider="fake_test", cookie_secure=False))
        self.assertIn("secure", str(raised.exception).lower())

        with self.assertRaises(RuntimeError) as raised:
            create_app(Settings(**base, auth_provider="hosted_oauth", cookie_secure=True))
        self.assertIn("not implemented", str(raised.exception).lower())

        # The test environment may use the fake provider (this suite proves it).
        self.assertEqual(self.a.get("/api/cloud/auth/session").status_code, 200)

    def test_candidate_budget_defaults(self):
        settings = Settings(database_url=self.database_url, secret_key="y" * 32)
        self.assertEqual(settings.chunk_target_ms, 3000)
        self.assertEqual(settings.saved_lag_target_ms, 10000)
        self.assertEqual(settings.live_latency_target_ms, (5000, 15000))
        self.assertEqual(settings.buffer_max_ms, 300000)
        self.assertEqual(settings.buffer_max_bytes, 100 * 1024 * 1024)
        self.assertEqual(settings.meeting_max_ms, 7200000)
        self.assertEqual(settings.active_capture_per_user, 1)
        self.assertEqual(settings.recovery_window_s, 86400)
        self.assertEqual(settings.cleanup_target_s, 86400)
        self.assertEqual(settings.ephemeral_cleanup_s, 3600)
        self.assertEqual(settings.backup_retention_days, 30)
        self.assertEqual(settings.log_retention_days, 30)
        self.assertEqual(settings.image_max_pixels, 20000000)
        self.assertEqual(settings.request_max_bytes, 30 * 1024 * 1024)
        self.assertEqual(settings.ai_output_max_chars, 40000)
        self.assertEqual(settings.request_timeout_s, 120)
        self.assertEqual(settings.worker_lease_s, 60)
        self.assertEqual(settings.worker_heartbeat_s, 20)
        self.assertEqual(IMAGE_MAX_BYTES, 5 * 1024 * 1024)

        with self.assertRaises(ValidationError):
            Settings(database_url=self.database_url, secret_key="y" * 32, chunk_target_ms=0)
        with self.assertRaises(ValidationError):
            Settings(
                database_url=self.database_url,
                secret_key="y" * 32,
                live_latency_target_ms=(15000, 5000),
            )
        with self.assertRaises(ValidationError):
            Settings(
                database_url=self.database_url,
                secret_key="y" * 32,
                worker_heartbeat_s=60,
                worker_lease_s=60,
            )
        with self.assertRaises(ValidationError):
            Settings(database_url=self.database_url, secret_key="short")
        with self.assertRaises(ValidationError):
            Settings(database_url="sqlite:///nope", secret_key="y" * 32)
