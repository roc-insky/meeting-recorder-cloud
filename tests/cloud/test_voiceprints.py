"""Task 13: owner-private, versioned voiceprints with synthetic extractors.

These tests never run a real speaker model, never claim calibrated thresholds
and never measure recognition accuracy. A fake extractor returns deterministic
unit vectors so the persistence, encryption, replacement and publish fences
can be checked against real PostgreSQL.
"""

from __future__ import annotations

import base64
import hashlib
import json
import unittest
from datetime import timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy import select, update

from cloud.contracts import (
    AuthContext,
    ConflictError,
    Embedding,
    InvalidRequestError,
    ModelMetadata,
    NotFoundError,
    PayloadTooLargeError,
    Segment,
    ServiceUnavailableError,
    Speaker,
    TaskView,
    TranscriptView,
)
from cloud.schema import (
    MeetingSpeaker,
    ProcessingTask,
    VoiceprintEnrollment,
    VoiceprintProfile,
    TranscriptSpeaker,
    TranscriptVersion,
)
from cloud.settings import Settings
from cloud.tasks import claim_task, publish_result
from cloud.voiceprints import (
    VOICEPRINT_EXTRACTION_FAILED,
    VOICEPRINT_INVALID_EMBEDDING,
    VOICEPRINT_KEY_MISSING,
    VOICEPRINT_SAMPLE_MISSING,
    VOICEPRINT_VERSION_CHANGED,
    _embedding_aad,
    _open,
    _sample_aad,
    cancel_voiceprint_task,
    enroll_profile,
    get_voiceprint_task,
    list_profiles,
    load_compatible_profiles,
    match_profiles,
    process_voiceprint_task,
    publish_voiceprint,
    recheck_voiceprint_usage,
    revoke_profile,
)
from cloud.worker import WorkItem, process_once
from tests.cloud.support import BASE_TIME, CloudCase
from tests.cloud import test_transcripts

KEY_BYTES = b"v" * 32
KEY = base64.urlsafe_b64encode(KEY_BYTES).decode("ascii")
OTHER_KEY = base64.urlsafe_b64encode(b"w" * 32).decode("ascii")
#: Minimal PCM/16-bit mono WAV header plus an empty data chunk. The server only
#: sniffs container signatures, so this is enough to prove real audio bytes pass
#: while an extension or upload content type never does.
WAV_SAMPLE = (
    b"RIFF\x24\x00\x00\x00WAVEfmt "
    b"\x10\x00\x00\x00\x01\x00\x01\x00"
    b"\x80\x3e\x00\x00\x00\x7d\x00\x00"
    b"\x02\x00\x10\x00data\x00\x00\x00\x00"
)
META = ModelMetadata(
    model_id="fake-voiceprint",
    model_version="1",
    weights_sha256="0" * 64,
    dimension=3,
    sample_rate=16000,
    preprocess_version="pcm16",
    normalization_version="l2",
    calibration_version="none",
)
ALT_META = META.model_copy(update={"model_version": "2"})
META8 = META.model_copy(update={"model_id": "fake-voiceprint-8", "dimension": 8})
META16 = META.model_copy(update={"model_id": "fake-voiceprint-16", "dimension": 16})


class FakeExtractor:
    """Deterministic synthetic extractor; no real model is involved."""

    def __init__(
        self,
        embedding: Embedding | None = None,
        *,
        error: Exception | None = None,
    ) -> None:
        self.embedding = embedding
        self.error = error
        self.audio: bytes | None = None

    def extract(self, audio: bytes) -> Embedding:
        self.audio = audio
        if self.error is not None:
            raise self.error
        assert self.embedding is not None
        return self.embedding


def unit(dimension: int, index: int = 0) -> list[float]:
    vector = [0.0] * dimension
    vector[index] = 1.0
    return vector


class VoiceprintsCase(CloudCase):
    maxDiff = None

    # -- fixtures ---------------------------------------------------------

    def vp_settings(self, **overrides: Any) -> Settings:
        data = self.settings.model_dump()
        data.update(
            voiceprint_key=KEY,
            voiceprint_high_threshold=0.9,
            voiceprint_low_threshold=0.5,
        )
        data.update(overrides)
        return Settings(**data)

    def context(
        self,
        db: Any,
        *,
        user_id: str | None = None,
        settings: Settings | None = None,
    ) -> AuthContext:
        return AuthContext(
            user_id=user_id or self.a_user_id,
            session_id="test-session",
            db=db,
            clock=self.clock,
            settings=settings or self.vp_settings(),
            objects=self.object_store,
        )

    def session(self) -> Any:
        db = self.session_factory()
        self.addCleanup(db.close)
        return db

    def enroll(
        self,
        ctx: AuthContext,
        name: str = "Alice Example",
        *,
        consent: str = "I consent to a private voiceprint for my own meetings.",
        audio: bytes = WAV_SAMPLE,
        replace_version: int | None = None,
        content_type: str | None = "audio/wav",
    ) -> TaskView:
        return enroll_profile(
            ctx,
            name,
            consent,
            audio,
            replace_version=replace_version,
            content_type=content_type,
        )

    def claim_next(self, ctx: AuthContext) -> Any:
        claimed = claim_task(
            "voiceprint-worker",
            self.clock.now(),
            db=ctx.db,
            settings=ctx.settings,
        )
        self.assertIsNotNone(claimed, "expected one claimable voiceprint task")
        assert claimed is not None
        return claimed

    def item(self, claimed: Any) -> WorkItem:
        return WorkItem(
            task_id=claimed.task.id,
            owner_user_id=claimed.task.owner_user_id,
            lease_token=claimed.lease_token,
            generation=claimed.task.generation,
            input_version=claimed.task.input_version,
            meeting_id=claimed.task.meeting_id,
        )

    def process_claimed(
        self,
        ctx: AuthContext,
        claimed: Any,
        extractor: FakeExtractor,
    ) -> bool:
        return process_voiceprint_task(
            ctx, self.item(claimed), extractor=extractor
        )

    def publish_profile(
        self,
        ctx: AuthContext,
        name: str = "Alice Example",
        *,
        vector: list[float] | None = None,
        metadata: ModelMetadata = META,
        audio: bytes = WAV_SAMPLE,
        replace_version: int | None = None,
        content_type: str | None = "audio/wav",
    ) -> tuple[TaskView, VoiceprintProfile]:
        embedding = Embedding(
            vector=vector or unit(metadata.dimension), metadata=metadata
        )
        task = self.enroll(
            ctx,
            name,
            audio=audio,
            replace_version=replace_version,
            content_type=content_type,
        )
        claimed = self.claim_next(ctx)
        self.assertTrue(
            self.process_claimed(ctx, claimed, FakeExtractor(embedding)),
            "expected the fake extraction to publish a profile",
        )
        profile = ctx.db.execute(
            select(VoiceprintProfile)
            .where(
                VoiceprintProfile.owner_user_id == ctx.user_id,
                VoiceprintProfile.name == name,
                VoiceprintProfile.revoked_at.is_(None),
            )
            .order_by(VoiceprintProfile.version.desc())
            .limit(1)
        ).scalar_one()
        return task, profile

    def meeting(self) -> str:
        return self.create_meeting(self.a, mode="mic", title="Voiceprint match")

    def seed_speaker(
        self,
        db: Any,
        meeting_id: str,
        vector: list[float],
        *,
        speaker_id: str = "spk-1",
        label: str = "说话人 1",
        metadata: ModelMetadata = META,
        owner_user_id: str | None = None,
    ) -> None:
        db.add(
            MeetingSpeaker(
                owner_user_id=owner_user_id or self.a_user_id,
                id=speaker_id,
                meeting_id=meeting_id,
                label=label,
                embedding=list(vector),
                **metadata.model_dump(mode="python"),
            )
        )
        db.commit()

    def transcript(
        self,
        meeting_id: str,
        *,
        speaker_id: str = "spk-1",
        label: str = "说话人 1",
    ) -> TranscriptView:
        return TranscriptView(
            id=str(uuid4()),
            meeting_id=meeting_id,
            segments=[
                Segment(
                    id="seg-1",
                    start_ms=0,
                    end_ms=1200,
                    text="synthetic transcript",
                    speaker_id=speaker_id,
                )
            ],
            speakers=[Speaker(id=speaker_id, label=label, suspect=False)],
            warnings=[],
            empty=False,
            status="complete",
        )

    def enrollment_row(self, db: Any, owner: str, enrollment_id: str) -> VoiceprintEnrollment:
        return db.execute(
            select(VoiceprintEnrollment).where(
                VoiceprintEnrollment.owner_user_id == owner,
                VoiceprintEnrollment.id == enrollment_id,
            )
        ).scalar_one()

    # -- owner privacy and version replacement ---------------------------

    def test_same_name_private_and_atomic_replace(self) -> None:
        concurrent = self.vp_settings(voiceprint_high_threshold=None, voiceprint_low_threshold=None)
        with self.session() as db:
            ctx_a = self.context(db, settings=concurrent)
            ctx_b = self.context(db, user_id=self.b_user_id, settings=concurrent)
            _, profile_a = self.publish_profile(
                ctx_a, "Shared Name", vector=unit(3, 0)
            )
            profile_a_id = profile_a.id
            _, profile_b = self.publish_profile(
                ctx_b, "Shared Name", vector=unit(3, 1)
            )
            profile_b_id = profile_b.id
            self.assertNotEqual(profile_a_id, profile_b_id)
            self.assertEqual([view.name for view in list_profiles(ctx_a)], ["Shared Name"])
            self.assertEqual([view.id for view in list_profiles(ctx_a)], [profile_a_id])
            self.assertEqual([view.id for view in list_profiles(ctx_b)], [profile_b_id])

            _, replaced = self.publish_profile(
                ctx_a,
                "Shared Name",
                vector=unit(3, 2),
                replace_version=1,
            )
            self.assertEqual(replaced.version, 2)
            self.assertNotEqual(replaced.id, profile_a_id)
            self.assertEqual(len(list_profiles(ctx_a)), 1)
            self.assertEqual(list_profiles(ctx_a)[0].id, replaced.id)
            self.assertEqual(len(list_profiles(ctx_b)), 1)
            self.assertEqual(list_profiles(ctx_b)[0].id, profile_b_id)

            old = db.get(VoiceprintProfile, (self.a_user_id, profile_a_id))
            self.assertIsNotNone(old.revoked_at)
            self.assertEqual(old.revocation_reason, "replaced")
            active_versions = db.execute(
                select(VoiceprintProfile.version).where(
                    VoiceprintProfile.owner_user_id == self.a_user_id,
                    VoiceprintProfile.name == "Shared Name",
                    VoiceprintProfile.revoked_at.is_(None),
                )
            ).scalars().all()
            self.assertEqual(active_versions, [2])

    def test_same_dimension_different_model_excluded(self) -> None:
        with self.session() as db:
            ctx = self.context(db)
            _, first = self.publish_profile(ctx, "Model One", vector=unit(3, 0))
            _, second = self.publish_profile(
                ctx, "Model Two", vector=unit(3, 0), metadata=ALT_META
            )
            self.assertEqual(first.version, second.version)
            self.assertEqual(first.dimension, second.dimension)

            compatible = load_compatible_profiles(ctx, META)
            self.assertEqual([profile.profile_id for profile in compatible], [first.id])
            alt = load_compatible_profiles(ctx, ALT_META)
            self.assertEqual([profile.profile_id for profile in alt], [second.id])

            meeting_id = self.meeting()
            self.seed_speaker(db, meeting_id, unit(3, 0), metadata=META)
            view = match_profiles(ctx, self.transcript(meeting_id))
            self.assertEqual([speaker.label for speaker in view.speakers], ["Model One"])
            self.assertEqual(len(view.voiceprint_usages), 1)
            self.assertEqual(view.voiceprint_usages[0].profile_id, first.id)

    def test_wrong_replace_version_and_duplicate_conflict(self) -> None:
        with self.session() as db:
            ctx = self.context(db)
            with self.assertRaises(ConflictError) as missing:
                self.enroll(ctx, "No Existing Profile", replace_version=1)
            self.assertEqual(missing.exception.code, "voiceprint_version_mismatch")
            self.publish_profile(ctx, "Replace Me", vector=unit(3, 0))

            with self.assertRaises(ConflictError) as duplicate:
                self.enroll(ctx, "Replace Me")
            self.assertEqual(duplicate.exception.code, "voiceprint_exists")
            with self.assertRaises(ConflictError) as stale:
                self.enroll(ctx, "Replace Me", replace_version=2)
            self.assertEqual(stale.exception.code, "voiceprint_version_mismatch")
            with self.assertRaises(InvalidRequestError) as older:
                self.enroll(ctx, "Replace Me", replace_version=0)
            self.assertEqual(older.exception.code, "invalid_voiceprint_version")

    def test_nfc_name_collision_conflicts(self) -> None:
        with self.session() as db:
            ctx = self.context(db)
            self.publish_profile(ctx, "José", vector=unit(3, 0))
            with self.assertRaises(ConflictError) as caught:
                self.enroll(ctx, "Jose\u0301")
            self.assertEqual(caught.exception.code, "voiceprint_exists")

    def test_consent_persisted_with_sample_audit(self) -> None:
        sample = WAV_SAMPLE
        with self.session() as db:
            ctx = self.context(db)
            task = self.enroll(
                ctx,
                "Audit Owner",
                consent="  I agree to this private enrolment.  ",
                audio=sample,
                content_type="audio/wav; codecs=pcm",
            )
            row = self.enrollment_row(db, self.a_user_id, db.execute(
                select(ProcessingTask.voiceprint_ref).where(
                    ProcessingTask.id == task.id
                )
            ).scalar_one())
            self.assertEqual(row.consent, "I agree to this private enrolment.")
            self.assertEqual(row.sample_bytes, len(sample))
            self.assertEqual(row.sample_sha256, hashlib.sha256(sample).hexdigest())
            self.assertEqual(row.content_type, "audio/wav")
            self.assertIsNotNone(row.captured_at)
            self.assertIsNotNone(row.sample_ciphertext)

            claimed = self.claim_next(ctx)
            self.assertTrue(self.process_claimed(
                ctx, claimed, FakeExtractor(Embedding(vector=unit(3), metadata=META))
            ))
            db.refresh(row)
            self.assertIsNone(row.sample_ciphertext)
            self.assertEqual(row.sample_bytes, len(sample))
            self.assertEqual(row.sample_sha256, hashlib.sha256(sample).hexdigest())
            self.assertEqual(row.consent, "I agree to this private enrolment.")
            self.assertIsNotNone(row.completed_at)

    # -- validation, encryption and task lifecycle -----------------------

    def test_invalid_name_consent_audio_and_oversize(self) -> None:
        with self.session() as db:
            ctx = self.context(db, settings=self.vp_settings(voiceprint_sample_max_bytes=64))
            cases = [
                ({"name": ""}, InvalidRequestError, "invalid_voiceprint_name"),
                ({"name": "x" * 101}, InvalidRequestError, "invalid_voiceprint_name"),
                ({"consent": "   "}, InvalidRequestError, "voiceprint_consent_required"),
                ({"audio": b""}, InvalidRequestError, "invalid_voiceprint_audio"),
                ({"replace_version": True}, InvalidRequestError, "invalid_voiceprint_version"),
        ]
            for overrides, expected, code in cases:
                with self.subTest(code=code):
                    kwargs: dict[str, Any] = {
                        "name": "Valid Name",
                        "consent": "valid consent",
                        "audio": WAV_SAMPLE,
                    }
                    kwargs.update(overrides)
                    with self.assertRaises(expected) as caught:
                        self.enroll(ctx, **kwargs)
                    self.assertEqual(caught.exception.code, code)
            with self.assertRaises(PayloadTooLargeError) as oversized:
                self.enroll(ctx, "Valid Name", audio=WAV_SAMPLE + b"x" * 100)
            self.assertEqual(oversized.exception.code, "voiceprint_sample_too_large")
            self.assertEqual(
                db.execute(select(VoiceprintEnrollment)).scalars().all(), []
            )
            self.assertEqual(db.execute(select(ProcessingTask)).scalars().all(), [])

    def test_missing_key_refuses_enrollment(self) -> None:
        with self.session() as db:
            ctx = self.context(
                db, settings=self.vp_settings(voiceprint_key=None)
            )
            with self.assertRaises(ServiceUnavailableError) as caught:
                self.enroll(ctx, "Alice")
            self.assertEqual(caught.exception.code, VOICEPRINT_KEY_MISSING)
            self.assertEqual(list_profiles(ctx), [])

    def test_samples_and_embeddings_encrypted_at_rest(self) -> None:
        sample = WAV_SAMPLE + b"never-store-this-plaintext-sample"
        with self.session() as db:
            ctx = self.context(db)
            task = self.enroll(ctx, "Encrypted", audio=sample)
            enrollment_id = db.execute(
                select(ProcessingTask.voiceprint_ref).where(
                    ProcessingTask.id == task.id
                )
            ).scalar_one()
            row = self.enrollment_row(db, self.a_user_id, enrollment_id)
            blob = row.sample_ciphertext
            assert blob is not None
            self.assertTrue(blob.startswith(b"VP1"))
            self.assertNotIn(sample, blob)
            self.assertEqual(
                _open(KEY_BYTES, blob, _sample_aad(self.a_user_id, enrollment_id)),
                sample,
            )
            for wrong_key, wrong_aad in (
                (b"x" * 32, _sample_aad(self.a_user_id, enrollment_id)),
                (KEY_BYTES, _sample_aad(self.b_user_id, enrollment_id)),
            ):
                with self.assertRaises(ValueError):
                    _open(wrong_key, blob, wrong_aad)

            _, profile = self.publish_profile(ctx, "Encrypted", vector=unit(3, 0))
            db.refresh(profile)
            embedding_blob = profile.embedding_ciphertext
            self.assertTrue(embedding_blob.startswith(b"VP1"))
            self.assertNotIn(b'"vector"', embedding_blob)
            plaintext = _open(
                KEY_BYTES,
                embedding_blob,
                _embedding_aad(self.a_user_id, profile.id, profile.version),
            )
            payload = json.loads(plaintext.decode("utf-8"))
            self.assertEqual(payload["vector"], unit(3, 0))
            with self.assertRaises(ValueError):
                _open(
                    b"x" * 32,
                    embedding_blob,
                    _embedding_aad(self.a_user_id, profile.id, profile.version),
                )

    def test_replacement_success_keeps_single_active_version(self) -> None:
        with self.session() as db:
            ctx = self.context(db)
            _, first = self.publish_profile(ctx, "Versioned", vector=unit(3, 0))
            _, second = self.publish_profile(
                ctx, "Versioned", vector=unit(3, 1), replace_version=1
            )
            self.assertEqual((first.version, second.version), (1, 2))
            self.assertIsNotNone(
                db.get(VoiceprintProfile, (self.a_user_id, first.id)).revoked_at
            )
            active = list_profiles(ctx)
            self.assertEqual([view.id for view in active], [second.id])
            self.assertEqual(active[0].version, 2)

    def test_extraction_and_invalid_embedding_fail_clears_sample(self) -> None:
        with self.session() as db:
            ctx = self.context(db)
            failed_task = self.enroll(ctx, "Extractor Fails", audio=WAV_SAMPLE + b"\x00")
            claimed = self.claim_next(ctx)
            self.assertFalse(
                self.process_claimed(
                    ctx, claimed, FakeExtractor(error=RuntimeError("boom"))
                )
            )
            task = db.get(ProcessingTask, failed_task.id)
            self.assertEqual(task.status, "failed")
            self.assertEqual(task.error_code, VOICEPRINT_EXTRACTION_FAILED)
            enrollment_id = task.voiceprint_ref
            row = self.enrollment_row(db, self.a_user_id, enrollment_id)
            self.assertIsNone(row.sample_ciphertext)
            self.assertEqual(row.error_code, VOICEPRINT_EXTRACTION_FAILED)

            invalid_task = self.enroll(ctx, "Invalid Vector", audio=WAV_SAMPLE + b"\x00\x01")
            claimed = self.claim_next(ctx)
            invalid = Embedding.model_construct(vector=[1.0, 0.0], metadata=META)
            self.assertFalse(
                self.process_claimed(ctx, claimed, FakeExtractor(invalid))
            )
            task = db.get(ProcessingTask, invalid_task.id)
            self.assertEqual(task.status, "failed")
            self.assertEqual(task.error_code, VOICEPRINT_INVALID_EMBEDDING)
            row = self.enrollment_row(db, self.a_user_id, task.voiceprint_ref)
            self.assertIsNone(row.sample_ciphertext)
            self.assertEqual(
                db.execute(select(VoiceprintProfile)).scalars().all(), []
            )

    def test_version_changed_during_processing_fails_without_corruption(self) -> None:
        with self.session() as db:
            ctx = self.context(db)
            _, first = self.publish_profile(ctx, "Race", vector=unit(3, 0))
            replacement_task = self.enroll(ctx, "Race", replace_version=1)
            claimed = self.claim_next(ctx)
            # A second replacement wins the race before this worker publishes.
            ctx.db.execute(
                update(VoiceprintProfile)
                .where(
                    VoiceprintProfile.owner_user_id == self.a_user_id,
                    VoiceprintProfile.id == first.id,
                )
                .values(version=2)
            )
            ctx.db.commit()
            self.assertFalse(
                self.process_claimed(
                    ctx, claimed, FakeExtractor(Embedding(vector=unit(3, 1), metadata=META))
                )
            )
            task = db.get(ProcessingTask, replacement_task.id)
            self.assertEqual(task.status, "failed")
            self.assertEqual(task.error_code, VOICEPRINT_VERSION_CHANGED)

    def test_cancel_pending_clears_sample_and_task(self) -> None:
        with self.session() as db:
            ctx = self.context(db)
            task = self.enroll(ctx, "Cancelled", audio=WAV_SAMPLE + b"\x02")
            enrollment_id = db.execute(
                select(ProcessingTask.voiceprint_ref).where(
                    ProcessingTask.id == task.id
                )
            ).scalar_one()
            cancel_voiceprint_task(ctx, task.id)
            row = db.get(ProcessingTask, task.id)
            self.assertEqual(row.status, "cancelled")
            self.assertEqual(row.error_code, "cancelled_by_user")
            enrollment = self.enrollment_row(db, self.a_user_id, enrollment_id)
            self.assertIsNone(enrollment.sample_ciphertext)
            self.assertEqual(enrollment.error_code, "cancelled_by_user")
            self.assertEqual(list_profiles(ctx), [])

    def test_expired_lease_retries_then_terminal_cleanup(self) -> None:
        with self.session() as db:
            ctx = self.context(db)
            task = self.enroll(ctx, "Expiring", audio=WAV_SAMPLE + b"\x03")
            claimed = self.claim_next(ctx)
            db.execute(
                update(ProcessingTask)
                .where(ProcessingTask.id == task.id)
                .values(
                    attempt=1,
                    lease_deadline=BASE_TIME - timedelta(seconds=1),
                )
            )
            db.commit()
            reclaimed = claim_task(
                "voiceprint-worker", self.clock.now(), db=db, settings=ctx.settings
            )
            self.assertIsNotNone(reclaimed)
            assert reclaimed is not None
            self.assertEqual(reclaimed.task.id, task.id)
            enrollment_id = db.execute(
                select(ProcessingTask.voiceprint_ref).where(
                    ProcessingTask.id == reclaimed.task.id
                )
            ).scalar_one()
            row = db.execute(
                select(ProcessingTask).where(ProcessingTask.id == task.id)
            ).scalar_one()
            self.assertEqual(row.status, "running")
            self.assertEqual(row.attempt, 2)
            enrollment = self.enrollment_row(
                db,
                self.a_user_id,
                db.execute(
                    select(ProcessingTask.voiceprint_ref).where(
                        ProcessingTask.id == task.id
                    )
                ).scalar_one(),
            )
            self.assertIsNotNone(enrollment.sample_ciphertext)

            db.execute(
                update(ProcessingTask)
                .where(ProcessingTask.id == task.id)
                .values(
                    attempt=3,
                    lease_deadline=BASE_TIME - timedelta(seconds=1),
                )
            )
            db.commit()
            self.assertIsNone(
                claim_task(
                    "voiceprint-worker",
                    self.clock.now(),
                    db=db,
                    settings=ctx.settings,
                )
            )
            row = db.get(ProcessingTask, task.id)
            self.assertEqual(row.status, "failed")
            self.assertEqual(row.error_code, "lease_expired")
            enrollment = self.enrollment_row(db, self.a_user_id, enrollment_id)
            self.assertIsNone(enrollment.sample_ciphertext)
            self.assertEqual(enrollment.error_code, "lease_expired")

    # -- owner isolation --------------------------------------------------

    def test_list_revoke_and_task_owner_isolation(self) -> None:
        with self.session() as db:
            ctx_a = self.context(db)
            ctx_b = self.context(db, user_id=self.b_user_id)
            task_a, profile_a = self.publish_profile(ctx_a, "Only Alice")
            self.publish_profile(ctx_b, "Only Bob")
            self.assertEqual([view.name for view in list_profiles(ctx_a)], ["Only Alice"])
            self.assertEqual([view.name for view in list_profiles(ctx_b)], ["Only Bob"])

            with self.assertRaises(NotFoundError):
                revoke_profile(ctx_b, profile_a.id)
            with self.assertRaises(NotFoundError):
                get_voiceprint_task(ctx_b, task_a.id)
            revoke_profile(ctx_a, profile_a.id)
            self.assertEqual(list_profiles(ctx_a), [])
            self.assertIsNotNone(
                db.get(VoiceprintProfile, (self.a_user_id, profile_a.id)).revoked_at
            )
            with self.assertRaises(NotFoundError):
                revoke_profile(ctx_a, profile_a.id)

    # -- matching boundaries ---------------------------------------------

    def test_high_threshold_relabels_without_suspect_flag(self) -> None:
        with self.session() as db:
            ctx = self.context(db)
            _, profile = self.publish_profile(ctx, "High Confidence")
            meeting_id = self.meeting()
            self.seed_speaker(db, meeting_id, unit(3, 0))
            view = match_profiles(ctx, self.transcript(meeting_id))
            self.assertEqual(view.speakers[0].label, "High Confidence")
            self.assertFalse(view.speakers[0].suspect)
            self.assertEqual(view.status, "complete")
            self.assertEqual(len(view.voiceprint_usages), 1)
            usage = view.voiceprint_usages[0]
            self.assertEqual(usage.anonymous_label, "说话人 1")
            self.assertEqual(usage.profile_id, profile.id)
            self.assertEqual(usage.profile_version, 1)

    def test_low_band_relabels_as_suspect_with_warning(self) -> None:
        with self.session() as db:
            ctx = self.context(db)
            self.publish_profile(ctx, "Almost Alice")
            meeting_id = self.meeting()
            self.seed_speaker(db, meeting_id, [0.6, 0.8, 0.0])
            view = match_profiles(ctx, self.transcript(meeting_id))
            self.assertEqual(view.speakers[0].label, "Almost Alice")
            self.assertTrue(view.speakers[0].suspect)
            self.assertIn("voiceprint_suspect_label", view.warnings)
            self.assertEqual(view.status, "completed_with_warnings")

    def test_below_low_and_unset_thresholds_leave_anonymous(self) -> None:
        with self.session() as db:
            ctx = self.context(db)
            self.publish_profile(ctx, "Far Away")
            meeting_id = self.meeting()
            self.seed_speaker(db, meeting_id, unit(3, 1))
            view = match_profiles(ctx, self.transcript(meeting_id))
            self.assertEqual(view.speakers[0].label, "说话人 1")
            self.assertEqual(view.voiceprint_usages, [])

            unset = self.context(
                db,
                settings=self.vp_settings(
                    voiceprint_high_threshold=None,
                    voiceprint_low_threshold=None,
                ),
            )
            self.seed_speaker(
                db, meeting_id, unit(3, 0), speaker_id="spk-2", label="说话人 2"
            )
            view = match_profiles(unset, self.transcript(meeting_id, speaker_id="spk-2", label="说话人 2"))
            self.assertEqual(view.speakers[0].label, "说话人 2")
            self.assertEqual(view.voiceprint_usages, [])

    def test_metadata_mismatch_and_cross_meeting_speaker_do_not_match(self) -> None:
        with self.session() as db:
            ctx = self.context(db)
            self.publish_profile(ctx, "Exact Metadata")
            meeting_a = self.meeting()
            meeting_b = self.meeting()
            self.seed_speaker(db, meeting_a, unit(3, 0), metadata=ALT_META)
            view = match_profiles(ctx, self.transcript(meeting_a))
            self.assertEqual(view.speakers[0].label, "说话人 1")
            self.assertEqual(view.voiceprint_usages, [])

            # The same speaker id exists only in another meeting: a transcript
            # from meeting A must never read meeting B's embedding.
            self.seed_speaker(db, meeting_b, unit(3, 0), speaker_id="cross-only")
            view = match_profiles(
                ctx, self.transcript(meeting_a, speaker_id="cross-only", label="说话人 2")
            )
            self.assertEqual(view.speakers[0].label, "说话人 2")
            self.assertEqual(view.voiceprint_usages, [])

    def test_fake_extractor_eight_and_sixteen_dimensions(self) -> None:
        with self.session() as db:
            ctx = self.context(db)
            _, profile8 = self.publish_profile(
                ctx,
                "Eight Dims",
                vector=unit(8),
                metadata=META8,
            )
            _, profile16 = self.publish_profile(
                ctx,
                "Sixteen Dims",
                vector=unit(16),
                metadata=META16,
            )
            self.assertEqual(profile8.dimension, 8)
            self.assertEqual(profile16.dimension, 16)
            self.assertEqual(len(load_compatible_profiles(ctx, META8)), 1)
            self.assertEqual(len(load_compatible_profiles(ctx, META16)), 1)
            meeting_id = self.meeting()
            self.seed_speaker(db, meeting_id, unit(8), metadata=META8)
            view = match_profiles(ctx, self.transcript(meeting_id))
            self.assertEqual(view.speakers[0].label, "Eight Dims")

    # -- publish re-check and worker integration -------------------------

    def test_revoke_before_publish_removes_new_real_name(self) -> None:
        meeting_id, input_version = test_transcripts.TranscriptsCase.sealed_media(self)
        with self.session() as db:
            ctx = self.context(db)
            self.seed_speaker(db, meeting_id, unit(3, 0))
            _, profile = self.publish_profile(ctx, "New Real Name")
            test_transcripts.TranscriptsCase.queue(self, meeting_id, input_version)
            claimed = self.claim_next(ctx)
            self.assertEqual(claimed.kind, "final")
            view = self.transcript(meeting_id)
            view = match_profiles(ctx, view)
            self.assertEqual(view.speakers[0].label, "New Real Name")

            revoke_profile(ctx, profile.id)
            self.assertTrue(
                publish_result(
                    claimed.task.id,
                    claimed.lease_token,
                    view,
                    owner_user_id=self.a_user_id,
                    now=self.clock.now(),
                    db=db,
                )
            )
            version = db.get(TranscriptVersion, (self.a_user_id, view.id))
            self.assertEqual(version.status, "completed_with_warnings")
            self.assertIn("voiceprint_revoked", version.warnings)
            persisted = db.execute(
                select(TranscriptSpeaker).where(
                    TranscriptSpeaker.owner_user_id == self.a_user_id,
                    TranscriptSpeaker.version_id == view.id,
                    TranscriptSpeaker.speaker_id == "spk-1",
                )
            ).scalar_one()
            self.assertEqual(persisted.label, "说话人 1")
            self.assertFalse(persisted.suspect)

    def test_direct_publish_without_voiceprint_settings_rechecks_safely(self) -> None:
        meeting_id, input_version = test_transcripts.TranscriptsCase.sealed_media(self)
        with self.session() as db:
            ctx = self.context(db)
            self.seed_speaker(db, meeting_id, unit(3, 0))
            _, profile = self.publish_profile(ctx, "Revoked At Publish")
            test_transcripts.TranscriptsCase.queue(self, meeting_id, input_version)
            claimed = self.claim_next(ctx)
            view = match_profiles(ctx, self.transcript(meeting_id))
            revoke_profile(ctx, profile.id)
            db.info.pop("voiceprint_settings", None)
            self.assertTrue(
                publish_result(
                    claimed.task.id,
                    claimed.lease_token,
                    view,
                    owner_user_id=self.a_user_id,
                    now=self.clock.now(),
                    db=db,
                )
            )
            persisted = db.execute(
                select(TranscriptSpeaker).where(
                    TranscriptSpeaker.owner_user_id == self.a_user_id,
                    TranscriptSpeaker.version_id == view.id,
                )
            ).scalar_one()
            self.assertEqual(persisted.label, "说话人 1")

    def test_final_worker_applies_enrolled_profile_before_publish(self) -> None:
        meeting_id, input_version = test_transcripts.TranscriptsCase.sealed_media(self)
        with self.session() as db:
            ctx = self.context(db)
            self.seed_speaker(db, meeting_id, unit(3, 0))
            self.publish_profile(ctx, "Worker Applied Name")

        test_transcripts.TranscriptsCase.queue(self, meeting_id, input_version)
        with self.session() as db:
            ctx = self.context(db)
            draft = self.transcript(meeting_id)

            def processor(item: Any, run_ctx: AuthContext) -> TranscriptView:
                self.assertEqual(item.meeting_id, meeting_id)
                self.assertEqual(run_ctx.user_id, self.a_user_id)
                return draft

            self.assertTrue(
                process_once(
                    processor,
                    worker_id="final-worker",
                    now=self.clock.now(),
                    db=db,
                    settings=ctx.settings,
                    context=ctx,
                )
            )
            persisted = db.execute(
                select(TranscriptSpeaker).where(
                    TranscriptSpeaker.owner_user_id == self.a_user_id,
                    TranscriptSpeaker.version_id == draft.id,
                )
            ).scalar_one()
            self.assertEqual(persisted.label, "Worker Applied Name")
            self.assertFalse(persisted.suspect)

    # -- HTTP API ---------------------------------------------------------

    def test_http_enrollment_validation_list_cancel_and_isolation(self) -> None:
        settings = self.vp_settings(voiceprint_sample_max_bytes=64)
        client = self.app_client(settings=settings)
        response = client.post(
            "/api/cloud/voiceprints",
            data={"name": "HTTP Name", "consent": "explicit consent"},
            files={"audio": ("sample.wav", WAV_SAMPLE, "audio/wav")},
            headers=self.csrf_headers(client),
        )
        self.assertEqual(response.status_code, 201, response.text)
        task = response.json()
        self.assertEqual(task["status"], "waiting")
        self.assertIsNone(task["meeting_id"])

        listed = client.get("/api/cloud/voiceprints")
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(listed.json(), [])
        fetched = client.get(f"/api/cloud/voiceprints/tasks/{task['id']}")
        self.assertEqual(fetched.status_code, 200, fetched.text)
        self.assertEqual(fetched.json()["id"], task["id"])

        no_csrf = client.post(
            "/api/cloud/voiceprints",
            data={"name": "No CSRF", "consent": "consent"},
            files={"audio": ("sample.wav", b"abc", "audio/wav")},
        )
        self.assertGreaterEqual(no_csrf.status_code, 400)

        for payload, expected in (
            (
                {"data": {"name": "No Consent"},
                 "files": {"audio": ("a.wav", b"abc", "audio/wav")}},
                400,
            ),
            (
                {"data": {"name": "Wrong Type", "consent": "ok"},
                 "files": {"audio": ("a.txt", b"abc", "text/plain")}},
                415,
            ),
            (
                {"data": {"name": "Too Large", "consent": "ok"},
                 "files": {"audio": ("a.wav", b"x" * 65, "audio/wav")}},
                413,
            ),
        ):
            with self.subTest(expected=expected):
                result = client.post(
                    "/api/cloud/voiceprints",
                    headers=self.csrf_headers(client),
                    **payload,
                )
                self.assertEqual(result.status_code, expected, result.text)

        cancelled = client.post(
            f"/api/cloud/voiceprints/tasks/{task['id']}/cancel",
            headers=self.csrf_headers(client),
        )
        self.assertEqual(cancelled.status_code, 204, cancelled.text)
        missing = client.delete(
            "/api/cloud/voiceprints/does-not-exist",
            headers=self.csrf_headers(client),
        )
        self.assertEqual(missing.status_code, 404, missing.text)

        no_key = self.app_client(
            settings=self.vp_settings(voiceprint_key=None)
        )
        unavailable = no_key.post(
            "/api/cloud/voiceprints",
            data={"name": "No Key", "consent": "ok"},
            files={"audio": ("a.wav", b"abc", "audio/wav")},
            headers=self.csrf_headers(no_key),
        )
        self.assertEqual(unavailable.status_code, 503, unavailable.text)
        self.assertEqual(unavailable.json()["error"], VOICEPRINT_KEY_MISSING)


if __name__ == "__main__":
    unittest.main()
