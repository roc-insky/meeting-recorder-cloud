"""Task 3 model-contract and cross-track identity tests.

No model weights, no real audio, no ffmpeg invocation as a measurement: the
identity fixture is driven by an injected synthetic scorer, and the probe must
report not_measured when the real Linux measurement environment is absent.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from spike.cloud_models import (
    ContractError,
    CrossTrackFixture,
    TrackUtterance,
    duplicate_pickup_fixture,
    evaluate_identity_fixture,
    evaluate_models,
    same_person_across_tracks_fixture,
    two_remote_speakers_fixture,
    validate_model_contract,
    voiceprint_compatible,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = (
    REPO_ROOT / "docs" / "superpowers" / "evidence" / "cloud" / "model-contract.json"
)
PROBE_RESULT_NAME = "model-probe-result.json"

VOICES = {
    "alice": (1.0, 0.0, 0.0, 0.0),
    "bob": (0.0, 1.0, 0.0, 0.0),
    "carol": (0.0, 0.0, 1.0, 0.0),
    "dave": (0.0, 0.0, 0.0, 1.0),
}


def synthetic_scorer(utterance):
    """Injected stand-in for a real embedding model: never a measurement."""
    return VOICES[utterance.voice_key]


def _complete_evidence():
    return {
        "metrics": {
            "cer": 0.18,
            "speaker_error_rate": 0.09,
            "latency_ms": 900,
        },
        "corpus": {
            "id": "authorised-cn-mixed-01",
            "description": "Authorised Chinese and Chinese/English meeting corpus.",
            "utterance_count": 120,
        },
        "package_versions": {"onnxruntime": "1.18.0", "ffmpeg": "6.0"},
        "weights_sha256": {
            "asr": "d" * 64,
            "embedding": "e" * 64,
        },
        "license": {
            "asr": {"name": "Apache-2.0", "spdx": "Apache-2.0", "redistributable": True}
        },
        "calibration": {
            "file": "calibration-high-low.json",
            "sha256": "f" * 64,
        },
    }


def _metadata(**overrides):
    metadata = {
        "model_id": "embedding-a",
        "model_version": "1",
        "weights_sha256": "a" * 64,
        "dimension": 512,
        "sample_rate": 16000,
        "preprocess_version": "pre-1",
        "normalization_version": "norm-1",
        "calibration_version": "cal-1",
    }
    metadata.update(overrides)
    return metadata


class ModelProbeCase(unittest.TestCase):
    def test_contract_rejects_unmeasured_strategy(self) -> None:
        contract = {
            "recognition": {
                "strategy": "approved",
                "approved_strategy": "mixed",
                "evidence": {
                    "metrics": {},
                    "corpus": {},
                    "package_versions": {},
                    "weights_sha256": {},
                    "license": {},
                    "calibration": {},
                },
            }
        }

        with self.assertRaises(ContractError) as raised:
            validate_model_contract(contract)

        message = str(raised.exception)
        for field in (
            "metrics",
            "corpus",
            "package_versions",
            "weights_sha256",
            "license",
            "calibration",
        ):
            self.assertIn(field, message)

    def test_cross_track_identity_fixture(self) -> None:
        fixtures = (
            same_person_across_tracks_fixture(),
            duplicate_pickup_fixture(),
            two_remote_speakers_fixture(),
        )
        for fixture in fixtures:
            evaluation = evaluate_identity_fixture(fixture, synthetic_scorer)
            self.assertEqual(
                evaluation.identity_count,
                fixture.expected_identity_count,
                fixture.name,
            )
            self.assertEqual(
                evaluation.utterance_count,
                fixture.expected_utterance_count,
                fixture.name,
            )
            self.assertEqual(
                evaluation.duplicate_count,
                fixture.expected_duplicate_count,
                fixture.name,
            )
            self.assertEqual(evaluation.scorer_calls, len(fixture.utterances))

        same_person = evaluate_identity_fixture(
            same_person_across_tracks_fixture(), synthetic_scorer
        )
        self.assertEqual(same_person.identity_count, 1)
        self.assertEqual(same_person.utterance_count, 2)
        self.assertEqual(same_person.duplicate_count, 0)
        self.assertEqual(same_person.identities[0].track_ids, ("mic", "system"))

        duplicate = evaluate_identity_fixture(
            duplicate_pickup_fixture(), synthetic_scorer
        )
        self.assertEqual(duplicate.identity_count, 1)
        self.assertEqual(duplicate.utterance_count, 1)
        self.assertEqual(duplicate.duplicate_count, 1)
        self.assertEqual(duplicate.dropped_utterance_ids, ("mic-1",))

        two_remote = evaluate_identity_fixture(
            two_remote_speakers_fixture(), synthetic_scorer
        )
        self.assertEqual(two_remote.identity_count, 2)
        self.assertEqual(two_remote.utterance_count, 2)
        self.assertEqual(
            sorted(identity.utterance_ids[0] for identity in two_remote.identities),
            ["system-1", "system-2"],
        )
        for identity in two_remote.identities:
            self.assertEqual(identity.track_ids, ("system",))

    def test_same_dimension_not_compatible(self) -> None:
        left = _metadata()
        right = _metadata(
            model_id="embedding-b",
            weights_sha256="b" * 64,
        )

        self.assertEqual(left["dimension"], right["dimension"])
        self.assertFalse(voiceprint_compatible(left, right))

        contract = {
            "recognition": {"strategy": "proposed_pending_measurement"},
            "voiceprint_compatibility": {
                "claim": {
                    "compatible": True,
                    "left": left,
                    "right": right,
                }
            },
        }

        with self.assertRaises(ContractError) as raised:
            validate_model_contract(contract)
        message = str(raised.exception).lower()
        self.assertIn("digest", message)
        self.assertIn("dimension", message)

    def test_duplicate_pickup_requires_time_window(self) -> None:
        fixture = CrossTrackFixture(
            name="same_text_far_apart",
            description="Same voice says the same words five seconds later.",
            utterances=(
                TrackUtterance("system-1", "system", 0, 2000, "同一句话。", "bob"),
                TrackUtterance("mic-1", "mic", 5000, 7000, "同一句话。", "bob"),
            ),
            expected_identity_count=1,
            expected_utterance_count=2,
            expected_duplicate_count=0,
        )
        evaluation = evaluate_identity_fixture(fixture, synthetic_scorer)
        self.assertEqual(evaluation.utterance_count, 2)
        self.assertEqual(evaluation.duplicate_count, 0)
        self.assertEqual(evaluation.identity_count, 1)

    def test_injected_scorer_is_required_and_errors_propagate(self) -> None:
        fixture = same_person_across_tracks_fixture()
        with self.assertRaises(TypeError):
            evaluate_identity_fixture(fixture, None)

        def broken_scorer(utterance):
            raise RuntimeError("model not loaded")

        with self.assertRaises(RuntimeError):
            evaluate_identity_fixture(fixture, broken_scorer)


class ContractValidationCase(unittest.TestCase):
    def test_contract_accepts_fully_evidenced_strategy(self) -> None:
        contract = {
            "recognition": {
                "strategy": "approved",
                "approved_strategy": "separate_merge",
                "evidence": _complete_evidence(),
            }
        }
        validate_model_contract(contract)

    def test_contract_rejects_approved_without_named_strategy(self) -> None:
        contract = {
            "recognition": {
                "strategy": "approved",
                "approved_strategy": None,
                "evidence": _complete_evidence(),
            }
        }
        with self.assertRaises(ContractError) as raised:
            validate_model_contract(contract)
        self.assertIn("approved_strategy", str(raised.exception))

    def test_contract_rejects_status_marker_instead_of_metrics(self) -> None:
        evidence = _complete_evidence()
        evidence["metrics"] = {"status": "not_measured", "cer": None}
        contract = {
            "recognition": {
                "strategy": "approved",
                "approved_strategy": "mixed",
                "evidence": evidence,
            }
        }
        with self.assertRaises(ContractError) as raised:
            validate_model_contract(contract)
        self.assertIn("metrics", str(raised.exception))

    def test_contract_rejects_each_missing_evidence_entry(self) -> None:
        for field in (
            "metrics",
            "corpus",
            "package_versions",
            "weights_sha256",
            "license",
            "calibration",
        ):
            evidence = _complete_evidence()
            evidence.pop(field)
            contract = {
                "recognition": {
                    "strategy": "approved",
                    "approved_strategy": "mixed",
                    "evidence": evidence,
                }
            }
            with self.assertRaises(ContractError, msg=field) as raised:
                validate_model_contract(contract)
            self.assertIn(field, str(raised.exception))

    def test_contract_rejects_incomplete_calibration_and_digest(self) -> None:
        evidence = _complete_evidence()
        evidence["calibration"] = {"file": "calibration-high-low.json"}
        with self.assertRaises(ContractError) as raised:
            validate_model_contract(
                {
                    "recognition": {
                        "strategy": "approved",
                        "approved_strategy": "mixed",
                        "evidence": evidence,
                    }
                }
            )
        self.assertIn("calibration", str(raised.exception))

        evidence = _complete_evidence()
        evidence["weights_sha256"] = {"asr": "not-a-digest"}
        with self.assertRaises(ContractError) as raised:
            validate_model_contract(
                {
                    "recognition": {
                        "strategy": "approved",
                        "approved_strategy": "mixed",
                        "evidence": evidence,
                    }
                }
            )
        self.assertIn("weights_sha256", str(raised.exception))

    def test_voiceprint_requires_full_metadata_match(self) -> None:
        metadata = _metadata()
        self.assertTrue(voiceprint_compatible(metadata, dict(metadata)))
        self.assertFalse(voiceprint_compatible(metadata, _metadata(dimension=256)))
        self.assertFalse(
            voiceprint_compatible(
                metadata, _metadata(calibration_version="cal-2")
            )
        )
        self.assertFalse(
            voiceprint_compatible(metadata, _metadata(weights_sha256=""))
        )

    def test_voiceprint_claim_with_missing_digest_is_rejected(self) -> None:
        contract = {
            "recognition": {"strategy": "proposed_pending_measurement"},
            "voiceprint_compatibility": {
                "claim": {
                    "compatible": True,
                    "left": _metadata(weights_sha256=""),
                    "right": _metadata(weights_sha256=""),
                }
            },
        }
        with self.assertRaises(ContractError) as raised:
            validate_model_contract(contract)
        self.assertIn("digest", str(raised.exception).lower())


class EvaluateModelsCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.dataset = self.root / "corpus"
        self.dataset.mkdir()
        (self.dataset / "labels.jsonl").write_text("{}\n", encoding="utf-8")
        self.out = self.root / "out"
        self.weights = self.root / "weights"
        self.weights.mkdir()
        (self.weights / "asr.onnx").write_bytes(b"weights")

    def _host(self, **overrides):
        host = {
            "system": "Linux",
            "machine": "x86_64",
            "python_version": "3.9.6",
            "ffmpeg_path": "/usr/bin/ffmpeg",
            "ffmpeg_version": "6.0-test",
            "compute_device": "cuda:0",
            "weights_dir": str(self.weights),
            "weights": [
                {
                    "name": "asr.onnx",
                    "bytes": 7,
                    "sha256": hashlib.sha256(b"weights").hexdigest(),
                }
            ],
            "package_versions": {"onnxruntime": "1.18.0"},
        }
        host.update(overrides)
        return host

    def _engine_result(self):
        return {
            "corpus": {
                "id": "synthetic-unit-fixture",
                "description": "Synthetic fixture used only to test plumbing.",
                "utterance_count": 4,
            },
            "license": {"asr": {"name": "test-only", "spdx": "MIT"}},
            "calibration": {"file": "cal.json", "sha256": "c" * 64},
            "strategies": {
                "mixed": {
                    "metrics": {
                        "cer": 0.20,
                        "speaker_error_rate": 0.10,
                        "latency_ms": 1200,
                    }
                },
                "separate_merge": {
                    "metrics": {
                        "cer": 0.15,
                        "speaker_error_rate": 0.12,
                        "latency_ms": 1500,
                    }
                },
            },
        }

    def test_not_measured_names_every_missing_prerequisite(self) -> None:
        result = evaluate_models(
            self.dataset,
            self.out,
            host=self._host(
                system="Darwin",
                machine="arm64",
                ffmpeg_path=None,
                ffmpeg_version=None,
                compute_device=None,
                weights_dir=None,
                weights=[],
            ),
        )

        self.assertEqual(result["status"], "not_measured")
        names = [item["prerequisite"] for item in result["missing_prerequisites"]]
        self.assertIn("linux_device", names)
        self.assertIn("weights", names)
        self.assertIn("ffmpeg", names)
        self.assertEqual(result["measurements"], {})
        self.assertEqual(
            result["recognition"]["strategy"], "proposed_pending_measurement"
        )

        written = self.out / PROBE_RESULT_NAME
        self.assertTrue(written.is_file())
        self.assertEqual(
            json.loads(written.read_text(encoding="utf-8")), result
        )

    def test_missing_dataset_is_named(self) -> None:
        result = evaluate_models(
            self.root / "absent-corpus",
            self.out,
            host=self._host(),
        )
        self.assertEqual(result["status"], "not_measured")
        names = [item["prerequisite"] for item in result["missing_prerequisites"]]
        self.assertEqual(names, ["dataset"])
        self.assertIn("absent-corpus", str(result["missing_prerequisites"][0]))

    def test_engine_never_runs_when_prerequisites_missing(self) -> None:
        calls = []

        def engine(dataset):
            calls.append(dataset)
            raise AssertionError("engine must not run without prerequisites")

        result = evaluate_models(
            self.dataset,
            self.out,
            host=self._host(system="Darwin", compute_device=None),
            engine=engine,
        )
        self.assertEqual(result["status"], "not_measured")
        self.assertEqual(calls, [])

    def test_complete_host_without_engine_is_not_measured(self) -> None:
        result = evaluate_models(self.dataset, self.out, host=self._host())
        self.assertEqual(result["status"], "not_measured")
        names = [item["prerequisite"] for item in result["missing_prerequisites"]]
        self.assertEqual(names, ["measurement_engine"])
        self.assertEqual(result["measurements"], {})

    def test_measured_metrics_are_copied_and_never_approved(self) -> None:
        observed = {}
        engine_result = self._engine_result()

        def engine(dataset):
            observed["dataset"] = dataset
            return engine_result

        result = evaluate_models(
            self.dataset,
            self.out,
            host=self._host(),
            engine=engine,
        )

        self.assertEqual(result["status"], "measured")
        self.assertEqual(observed["dataset"], self.dataset)
        self.assertEqual(
            result["measurements"]["strategies"],
            engine_result["strategies"],
        )
        self.assertEqual(
            result["recognition"]["strategy"], "proposed_pending_measurement"
        )
        self.assertEqual(
            result["recognition"]["decision"], "pending_user_approved_gate"
        )
        self.assertIsNone(result["recognition"]["approved_strategy"])
        deltas = result["recognition"]["comparison"]["metric_deltas"]
        self.assertAlmostEqual(deltas["cer"], -0.05, places=6)
        self.assertAlmostEqual(deltas["speaker_error_rate"], 0.02, places=6)
        self.assertNotEqual(result["measurements"], {})
        self.assertTrue((self.out / PROBE_RESULT_NAME).is_file())

    def test_engine_missing_a_strategy_fails_honestly(self) -> None:
        result = evaluate_models(
            self.dataset,
            self.out,
            host=self._host(),
            engine=lambda dataset: {
                "strategies": {
                    "mixed": {"metrics": {"cer": 0.2}},
                }
            },
        )
        self.assertEqual(result["status"], "measurement_failed")
        self.assertEqual(result["measurements"], {})
        self.assertIn("separate_merge", result["engine_error"]["message"])

    def test_engine_crash_records_no_metrics(self) -> None:
        def engine(dataset):
            raise RuntimeError("model failed to load")

        result = evaluate_models(
            self.dataset,
            self.out,
            host=self._host(),
            engine=engine,
        )
        self.assertEqual(result["status"], "measurement_failed")
        self.assertEqual(result["measurements"], {})
        self.assertEqual(result["engine_error"]["type"], "RuntimeError")
        self.assertIn("model failed to load", result["engine_error"]["message"])

    def test_default_call_on_this_host_reports_not_measured(self) -> None:
        result = evaluate_models(self.dataset, self.out)
        self.assertEqual(result["status"], "not_measured")
        self.assertEqual(result["measurements"], {})
        self.assertTrue(result["missing_prerequisites"])
        self.assertTrue((self.out / PROBE_RESULT_NAME).is_file())


class CommittedContractCase(unittest.TestCase):
    def setUp(self) -> None:
        self.contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))

    def test_committed_contract_is_valid_and_not_approved(self) -> None:
        validate_model_contract(self.contract)
        recognition = self.contract["recognition"]
        self.assertEqual(
            recognition["strategy"], "proposed_pending_measurement"
        )
        self.assertIsNone(recognition["approved_strategy"])
        self.assertEqual(recognition["evidence_status"], "not_measured")
        self.assertIsNone(recognition["evidence"])
        self.assertEqual(self.contract["status"], "not_measured")

    def test_committed_contract_keeps_proposed_parameters_and_empty_metrics(self) -> None:
        comparison = self.contract["comparison_plan"]
        self.assertEqual(
            set(comparison["strategies"]), {"mixed", "separate_merge"}
        )
        self.assertEqual(comparison["gate"]["status"], "pending_user_approval")
        for name in (
            "dedup_window_ms",
            "dedup_min_text_similarity",
            "cluster_similarity_threshold",
        ):
            parameter = self.contract["proposed_parameters"][name]
            self.assertEqual(parameter["status"], "proposed")
            self.assertTrue(parameter["rationale"])
        self.assertEqual(self.contract["measurements"], {})
        self.assertEqual(
            self.contract["voiceprint_compatibility"]["claim"]["compatible"], False
        )
        for engine in self.contract["engines"].values():
            self.assertEqual(engine["status"], "not_measured")
            self.assertIsNone(engine["weights_sha256"])

    def test_fixture_expectations_match_contract(self) -> None:
        documented = self.contract["cross_track_identity_expectations"]
        for fixture in (
            same_person_across_tracks_fixture(),
            duplicate_pickup_fixture(),
            two_remote_speakers_fixture(),
        ):
            expected = documented[fixture.name]
            self.assertEqual(
                expected["identity_count"], fixture.expected_identity_count
            )
            self.assertEqual(
                expected["utterance_count"], fixture.expected_utterance_count
            )
            self.assertEqual(
                expected["duplicate_count"], fixture.expected_duplicate_count
            )


if __name__ == "__main__":
    unittest.main()
