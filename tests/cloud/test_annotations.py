"""Task 12A: owner-scoped, meeting-relative annotations.

The last class here proves the real migration path (fresh schema, head, base,
head) rather than the create_all shortcut the HTTP cases use, and checks that
annotations carry no transcript-version coordinate of any kind.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from uuid import uuid4

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from cloud.db import reset_schema
from tests.cloud.support import BASE_TIME, CloudCase

ROOT = Path(__file__).resolve().parents[2]


class AnnotationsCase(CloudCase):
    def setUp(self) -> None:
        super().setUp()
        self.meeting_id = self.create_meeting(self.a)

    # -- helpers -----------------------------------------------------------

    def post_annotation(
        self,
        client,
        meeting_id: str | None = None,
        *,
        kind="note",
        offset_ms=0,
        text="hello",
        headers: dict[str, str] | None = None,
    ):
        if meeting_id is None:
            meeting_id = self.meeting_id
        return client.post(
            f"/api/cloud/meetings/{meeting_id}/annotations",
            json={"kind": kind, "offset_ms": offset_ms, "text": text},
            headers=self.csrf_headers(client) if headers is None else headers,
        )

    def list_annotations(self, client, meeting_id: str | None = None):
        if meeting_id is None:
            meeting_id = self.meeting_id
        return client.get(f"/api/cloud/meetings/{meeting_id}/annotations")

    def insert_transcript_version(self, meeting_id: str, generation: int) -> str:
        version_id = str(uuid4())
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO transcript_versions"
                    " (owner_user_id, id, meeting_id, task_id, input_version,"
                    " generation, payload, status, empty, warnings, created_at)"
                    " VALUES (:owner, :id, :meeting, NULL, 0, :generation,"
                    " '{}'::jsonb, 'complete', true, '[]'::jsonb, :created_at)"
                ),
                {
                    "owner": self.a_user_id,
                    "id": version_id,
                    "meeting": meeting_id,
                    "generation": generation,
                    "created_at": self.clock.now(),
                },
            )
            connection.execute(
                text(
                    "UPDATE meetings SET current_transcript_id = :version"
                    " WHERE owner_user_id = :owner AND id = :meeting"
                ),
                {
                    "version": version_id,
                    "owner": self.a_user_id,
                    "meeting": meeting_id,
                },
            )
        return version_id

    # -- roundtrip and ordering -------------------------------------------

    def test_roundtrip_trims_text_and_returns_view(self) -> None:
        response = self.post_annotation(
            self.a, kind="highlight", offset_ms=12345, text="  decision: ship it  "
        )
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual(
            set(body), {"id", "meeting_id", "offset_ms", "kind", "text"}
        )
        self.assertEqual(body["meeting_id"], self.meeting_id)
        self.assertEqual(body["offset_ms"], 12345)
        self.assertEqual(body["kind"], "highlight")
        self.assertEqual(body["text"], "decision: ship it")

        self.assertEqual(
            self.query_value(
                "SELECT text FROM annotations WHERE id = :id", id=body["id"]
            ),
            "decision: ship it",
        )
        self.assertEqual(
            self.query_value(
                "SELECT created_at FROM annotations WHERE id = :id",
                id=body["id"],
            ),
            BASE_TIME,
        )

        listed = self.list_annotations(self.a)
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(listed.json(), [body])

    def test_list_orders_by_offset_then_created_at_then_id(self) -> None:
        late = self.post_annotation(self.a, offset_ms=2000, text="late").json()
        # Same offset and same frozen created_at: the id is the tie-break.
        first = self.post_annotation(self.a, offset_ms=1000, text="first").json()
        second = self.post_annotation(self.a, offset_ms=1000, text="second").json()
        early = self.post_annotation(self.a, offset_ms=0, text="early").json()

        listed = self.list_annotations(self.a)
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(
            [row["id"] for row in listed.json()],
            [early["id"]] + sorted([first["id"], second["id"]]) + [late["id"]],
        )
        self.assertEqual(
            [row["offset_ms"] for row in listed.json()],
            [0, 1000, 1000, 2000],
        )
        self.assertEqual(
            self.query_value(
                "SELECT count(DISTINCT created_at) FROM annotations"
                " WHERE owner_user_id = :owner",
                owner=self.a_user_id,
            ),
            1,
        )

    # -- timeline, not version --------------------------------------------

    def test_retranscribe_keeps_offset(self) -> None:
        version_one = self.insert_transcript_version(self.meeting_id, 0)
        created = self.post_annotation(
            self.a, kind="comment", offset_ms=12345, text="needs a decision"
        )
        self.assertEqual(created.status_code, 201, created.text)
        annotation_id = created.json()["id"]

        self.clock.advance(seconds=30)
        version_two = self.insert_transcript_version(self.meeting_id, 1)
        self.assertNotEqual(version_one, version_two)

        self.assertEqual(
            self.query_value(
                "SELECT current_transcript_id FROM meetings"
                " WHERE owner_user_id = :owner AND id = :meeting",
                owner=self.a_user_id,
                meeting=self.meeting_id,
            ),
            version_two,
        )
        versions = self.a.get(
            f"/api/cloud/meetings/{self.meeting_id}/transcripts"
        )
        self.assertEqual(versions.status_code, 200, versions.text)
        self.assertEqual(
            {row["id"] for row in versions.json()},
            {version_one, version_two},
        )

        listed = self.list_annotations(self.a)
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(len(listed.json()), 1)
        self.assertEqual(listed.json()[0]["id"], annotation_id)
        self.assertEqual(listed.json()[0]["offset_ms"], 12345)
        self.assertEqual(
            self.query_value(
                "SELECT count(*) FROM information_schema.columns"
                " WHERE table_name = 'annotations'"
                " AND column_name LIKE '%version%'"
            ),
            0,
        )

    # -- validation --------------------------------------------------------

    def test_rejects_invalid_payloads_and_stores_nothing(self) -> None:
        cases = [
            ("kind", {"kind": "todo"}),
            ("kind type", {"kind": 3}),
            ("kind null", {"kind": None}),
            ("negative offset", {"offset_ms": -1}),
            ("bool offset", {"offset_ms": True}),
            ("float offset", {"offset_ms": 1.5}),
            ("string offset", {"offset_ms": "100"}),
            ("blank text", {"text": ""}),
            ("whitespace text", {"text": "   "}),
            ("text type", {"text": 5}),
            ("too long", {"text": "x" * 2001}),
        ]
        for label, override in cases:
            payload = {"kind": "note", "offset_ms": 0, "text": "hello"}
            payload.update(override)
            with self.subTest(label):
                response = self.a.post(
                    f"/api/cloud/meetings/{self.meeting_id}/annotations",
                    json=payload,
                    headers=self.csrf_headers(self.a),
                )
                self.assertEqual(response.status_code, 400, response.text)
                self.assertEqual(response.json(), {"error": "invalid_annotation"})
                self.assertEqual(self.query_count("annotations"), 0)

        boundary = self.post_annotation(self.a, text="y" * 2000)
        self.assertEqual(boundary.status_code, 201, boundary.text)
        self.assertEqual(len(boundary.json()["text"]), 2000)

    # -- ownership and existence ------------------------------------------

    def test_other_owner_cannot_see_or_add(self) -> None:
        self.post_annotation(self.a, text="private")

        read = self.list_annotations(self.b)
        self.assertEqual(read.status_code, 404, read.text)
        self.assertEqual(read.json(), {"error": "not_found"})
        write = self.post_annotation(self.b, text="intrusion")
        self.assertEqual(write.status_code, 404, write.text)
        self.assertEqual(write.json(), {"error": "not_found"})
        self.assertEqual(self.query_count("annotations"), 1)

    def test_deleted_meeting_is_404(self) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE meetings SET deleted_at = now()"
                    " WHERE owner_user_id = :owner AND id = :meeting"
                ),
                {"owner": self.a_user_id, "meeting": self.meeting_id},
            )

        read = self.list_annotations(self.a)
        self.assertEqual(read.status_code, 404, read.text)
        write = self.post_annotation(self.a, text="after delete")
        self.assertEqual(write.status_code, 404, write.text)
        self.assertEqual(self.query_count("annotations"), 0)

    def test_unknown_meeting_is_404(self) -> None:
        missing = str(uuid4())
        for response in (
            self.list_annotations(self.a, missing),
            self.post_annotation(self.a, missing, text="nowhere"),
        ):
            self.assertEqual(response.status_code, 404, response.text)
            self.assertEqual(response.json(), {"error": "not_found"})

    def test_list_is_owner_scoped(self) -> None:
        mine = self.post_annotation(self.a, offset_ms=10, text="mine").json()
        b_meeting = self.create_meeting(self.b)
        theirs = self.post_annotation(
            self.b, b_meeting, offset_ms=20, text="theirs"
        ).json()

        a_list = self.list_annotations(self.a).json()
        b_list = self.list_annotations(self.b, b_meeting).json()
        self.assertEqual([row["id"] for row in a_list], [mine["id"]])
        self.assertEqual([row["id"] for row in b_list], [theirs["id"]])

    # -- auth --------------------------------------------------------------

    def test_post_requires_csrf(self) -> None:
        response = self.post_annotation(
            self.a, text="no token", headers={}
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.query_count("annotations"), 0)

        allowed = self.post_annotation(self.a, text="with token")
        self.assertEqual(allowed.status_code, 201, allowed.text)

    def test_list_requires_session(self) -> None:
        response = self.list_annotations(self.anon)
        self.assertEqual(response.status_code, 401, response.text)


class AnnotationsMigrationCase(unittest.TestCase):
    """Run the real migration chain against a fresh schema, both directions."""

    @classmethod
    def setUpClass(cls) -> None:
        url = os.environ.get("TEST_DATABASE_URL")
        if not url:
            raise RuntimeError(
                "TEST_DATABASE_URL must point at the disposable cloud_test PostgreSQL database"
            )
        database = url.rsplit("/", 1)[-1].split("?", 1)[0]
        if "test" not in database:
            raise RuntimeError(f"refusing to reset non-test database {database!r}")
        cls.database_url = url
        cls.engine = sa.create_engine(url, pool_pre_ping=True)
        reset_schema(cls.engine)
        # env.py prefers CLOUD_DATABASE_URL; never let a developer's real
        # database be touched by migration tests.
        cls.saved_cloud_url = os.environ.pop("CLOUD_DATABASE_URL", None)
        cls.config = Config(str(ROOT / "alembic.ini"))
        cls.config.set_main_option(
            "script_location", str(ROOT / "cloud" / "migrations")
        )
        command.upgrade(cls.config, "head")

    @classmethod
    def tearDownClass(cls) -> None:
        reset_schema(cls.engine)
        cls.engine.dispose()
        if cls.saved_cloud_url is not None:
            os.environ["CLOUD_DATABASE_URL"] = cls.saved_cloud_url

    def test_roundtrip_and_metadata_match(self) -> None:
        inspector = sa.inspect(self.engine)
        self.assertIn("annotations", inspector.get_table_names())
        self.assertEqual(
            self.query_value("SELECT version_num FROM alembic_version"),
            "0010_positions",
        )

        columns = {row["name"]: row for row in inspector.get_columns("annotations")}
        self.assertEqual(
            set(columns),
            {
                "id",
                "owner_user_id",
                "meeting_id",
                "kind",
                "offset_ms",
                "text",
                "created_at",
            },
        )
        self.assertTrue(all(not row["nullable"] for row in columns.values()))

        foreign_keys = {
            row["name"]: row for row in inspector.get_foreign_keys("annotations")
        }
        self.assertEqual(
            set(foreign_keys),
            {"fk_annotations_owner", "fk_annotations_meeting"},
        )
        self.assertEqual(
            foreign_keys["fk_annotations_meeting"]["constrained_columns"],
            ["owner_user_id", "meeting_id"],
        )
        self.assertEqual(
            foreign_keys["fk_annotations_meeting"]["referred_table"], "meetings"
        )
        self.assertEqual(
            foreign_keys["fk_annotations_meeting"]["referred_columns"],
            ["owner_user_id", "id"],
        )
        self.assertEqual(
            foreign_keys["fk_annotations_meeting"]["options"].get("ondelete"),
            "CASCADE",
        )
        self.assertEqual(
            foreign_keys["fk_annotations_owner"]["options"].get("ondelete"),
            "CASCADE",
        )

        indexes = {
            row["name"]: row for row in inspector.get_indexes("annotations")
        }
        self.assertIn("ix_annotations_owner_meeting_offset", indexes)
        self.assertEqual(
            indexes["ix_annotations_owner_meeting_offset"]["column_names"],
            ["owner_user_id", "meeting_id", "offset_ms", "id"],
        )

        checks = {
            row[0]
            for row in self.engine.connect().execute(
                text(
                    "SELECT conname FROM pg_constraint"
                    " WHERE conrelid = 'annotations'::regclass AND contype = 'c'"
                )
            )
        }
        self.assertEqual(
            checks,
            {
                "ck_annotations_kind",
                "ck_annotations_offset",
                "ck_annotations_text",
            },
        )

        # Metadata and migration must agree, not merely coexist.
        command.check(self.config)

        command.downgrade(self.config, "base")
        self.assertNotIn(
            "annotations", sa.inspect(self.engine).get_table_names()
        )

        command.upgrade(self.config, "head")
        self.assertIn("annotations", sa.inspect(self.engine).get_table_names())

    def query_value(self, sql: str):
        with self.engine.connect() as connection:
            return connection.execute(text(sql)).scalar_one_or_none()
