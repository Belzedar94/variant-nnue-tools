from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "script" / "atomic_v3_reachability_oracle.py"
FEATURE_SCHEMA = ROOT / "spec" / "atomic-nnue-v3.json"
ORACLE_LOCK = ROOT / "spec" / "atomic-v3-reachability-oracle.lock.json"
ATTESTATION_SCHEMA = ROOT / "spec" / "atomic-v3-reachability-attestation-v1.json"
GOLDEN_PATH = ROOT / "tests" / "fixtures" / "atomic_v3_reachability_v1.json"

SPEC = importlib.util.spec_from_file_location("atomic_v3_reachability_oracle", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
ORACLE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = ORACLE
SPEC.loader.exec_module(ORACLE)

GOLDEN = json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


def bit(bitmap: bytes, index: int) -> int:
    return (bitmap[index // 8] >> (index % 8)) & 1


def canonical(value: dict) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


def run_cli(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *arguments],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def digest_map(result: ORACLE.OracleResult) -> dict[str, dict[str, str]]:
    return {
        perspective: {
            field: result.roles[perspective][field]["sha256"]
            for field in ORACLE.MASK_FIELDS + ORACLE.DERIVED_FIELDS
        }
        for perspective in ORACLE.PERSPECTIVES
    }


class OracleGoldenTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.result = ORACLE.generate_oracle_result(FEATURE_SCHEMA)
        cls.masks = {}
        for item in cls.result.physical_layout[:4]:
            offset = item["offset"]
            cls.masks[item["slice"]] = cls.result.output[
                offset : offset + item["bytes"]
            ]

    def test_vendored_contracts_are_exact_upstream_bytes(self) -> None:
        self.assertEqual(
            hashlib.sha256(FEATURE_SCHEMA.read_bytes()).hexdigest(),
            ORACLE.FEATURE_SCHEMA_SHA256,
        )
        self.assertEqual(
            hashlib.sha256(ATTESTATION_SCHEMA.read_bytes()).hexdigest(),
            ORACLE.REACHABILITY_SCHEMA_SHA256,
        )
        self.assertEqual(
            ORACLE.UPSTREAM_CONTRACT_COMMIT,
            "dde43fc08fb2bd45eec09d3be9f6d06845eeb24",
        )

        lock = json.loads(ORACLE_LOCK.read_text(encoding="utf-8"))
        self.assertEqual(lock["source"]["commit"], ORACLE.UPSTREAM_CONTRACT_COMMIT)
        self.assertEqual(lock["feature_schema"]["bytes"], FEATURE_SCHEMA.stat().st_size)
        self.assertEqual(lock["feature_schema"]["sha256"], ORACLE.FEATURE_SCHEMA_SHA256)
        self.assertEqual(
            lock["reachability_attestation_schema"]["bytes"],
            ATTESTATION_SCHEMA.stat().st_size,
        )
        self.assertEqual(
            lock["reachability_attestation_schema"]["sha256"],
            ORACLE.REACHABILITY_SCHEMA_SHA256,
        )

    def test_complete_wire_and_aggregate_match_frozen_golden(self) -> None:
        self.assertEqual(len(self.result.output), 18_772)
        self.assertEqual(
            hashlib.sha256(self.result.output).hexdigest(),
            GOLDEN["oracle_output_sha256"],
        )
        self.assertEqual(
            self.result.aggregate_sha256, GOLDEN["reachability_mask_sha256"]
        )
        self.assertEqual(digest_map(self.result), GOLDEN["per_mask_sha256"])

    def test_each_physical_and_derived_mask_matches_frozen_golden(self) -> None:
        for name, expected in GOLDEN["physical"].items():
            mask = self.masks[name]
            self.assertEqual(len(mask), expected["bytes"], name)
            self.assertEqual(sum(byte.bit_count() for byte in mask), expected["reachable_indices"], name)
            self.assertEqual(hashlib.sha256(mask).hexdigest(), expected["raw_sha256"], name)
        training, virtual = ORACLE.derive_hm_masks(self.masks[ORACLE.SLICE_IDS[0]])
        for name, mask in (("hm_training", training), ("hm_virtual_factors", virtual)):
            expected = GOLDEN["derived_hm"][name]
            self.assertEqual(len(mask), expected["bytes"], name)
            self.assertEqual(sum(byte.bit_count() for byte in mask), expected["reachable_indices"], name)
            self.assertEqual(hashlib.sha256(mask).hexdigest(), expected["raw_sha256"], name)

    def test_white_then_black_wire_order_is_exact(self) -> None:
        self.assertEqual(self.result.output[:9_386], self.result.output[9_386:])
        expected_offsets = [0, 2_816, 7_818, 8_106, 9_386, 12_202, 17_204, 17_492]
        self.assertEqual(
            [entry["offset"] for entry in self.result.physical_layout], expected_offsets
        )

    def test_hm_symbolic_impossibilities(self) -> None:
        hm = self.masks[ORACLE.SLICE_IDS[0]]
        bucket = 0  # Oriented own king h8.
        own_king = 63
        own_knight = (bucket * 11 + 2) * 64 + own_king
        merged_king = (bucket * 11 + 10) * 64 + own_king
        own_pawn_a1 = (bucket * 11) * 64
        own_pawn_e4 = (bucket * 11) * 64 + 28
        self.assertEqual(bit(hm, own_knight), 0)
        self.assertEqual(bit(hm, merged_king), 1)
        self.assertEqual(bit(hm, own_pawn_a1), 0)
        self.assertEqual(bit(hm, own_pawn_e4), 1)

    def test_capture_pair_promotion_and_oriented_king_constraints(self) -> None:
        capture = self.masks[ORACLE.SLICE_IDS[1]]
        own_geometry = ORACLE._capture_geometry(0)
        pawn_ordinal = own_geometry["PAWN"].index((48, 57))  # a7xb8
        pawn_target = pawn_ordinal * 6
        knight_target = pawn_target + 1
        self.assertEqual(bit(capture, pawn_target), 0)
        self.assertEqual(bit(capture, knight_target), 1)

        opp_geometry = ORACLE._capture_geometry(1)
        rook_base = 980
        to_b1 = rook_base + opp_geometry["ROOK"].index((0, 1))
        to_e1 = rook_base + opp_geometry["ROOK"].index((0, 4))
        self.assertEqual(bit(capture, (3_332 + to_b1) * 6 + 5), 0)
        self.assertEqual(bit(capture, (3_332 + to_e1) * 6 + 5), 1)
        self.assertEqual(bit(capture, to_b1 * 6 + 5), 1)
        self.assertTrue(all(bit(capture, index) for index in range(39_984, 40_012)))

    def test_king_blast_ep_offboard_orientation_and_ep_ranks(self) -> None:
        mask = self.masks[ORACLE.SLICE_IDS[2]]
        classes = [
            "ENEMY_KING_CENTER",
            *["ENEMY_KING_" + name for name, _df, _dr in ORACLE.DIRECTIONS],
            *["OWN_KING_" + name for name, _df, _dr in ORACLE.DIRECTIONS],
            "EN_PASSANT_MARKER",
        ]

        def index(center: int, actor: int, name: str) -> int:
            return (center * 2 + actor) * 18 + classes.index(name)

        self.assertEqual(bit(mask, index(0, 0, "ENEMY_KING_N")), 1)
        self.assertEqual(bit(mask, index(0, 0, "ENEMY_KING_S")), 0)
        self.assertEqual(bit(mask, index(0, 1, "ENEMY_KING_N")), 0)
        self.assertEqual(bit(mask, index(3, 1, "ENEMY_KING_E")), 1)
        self.assertEqual(bit(mask, index(3, 0, "OWN_KING_E")), 1)
        self.assertEqual(bit(mask, index(0, 0, "OWN_KING_E")), 0)
        self.assertEqual(bit(mask, index(27, 1, "ENEMY_KING_CENTER")), 0)
        self.assertEqual(bit(mask, index(28, 1, "ENEMY_KING_CENTER")), 1)
        self.assertEqual(bit(mask, index(40, 0, "EN_PASSANT_MARKER")), 1)
        self.assertEqual(bit(mask, index(32, 0, "EN_PASSANT_MARKER")), 0)
        self.assertEqual(bit(mask, index(16, 1, "EN_PASSANT_MARKER")), 1)

    def test_blast_ring_offboard_and_pawn_back_rank_constraints(self) -> None:
        mask = self.masks[ORACLE.SLICE_IDS[3]]
        directions = [name for name, _df, _dr in ORACLE.DIRECTIONS]
        classes = ["KNIGHT", "BISHOP", "ROOK", "QUEEN", "ADJACENT_PAWN_SURVIVES"]

        def index(center: int, actor: int, collateral: int, direction: str, piece: str) -> int:
            return (
                ((((center * 2 + actor) * 2 + collateral) * 8 + directions.index(direction)) * 5)
                + classes.index(piece)
            )

        self.assertEqual(bit(mask, index(0, 0, 0, "S", "KNIGHT")), 0)
        self.assertEqual(bit(mask, index(0, 0, 0, "N", "ADJACENT_PAWN_SURVIVES")), 1)
        self.assertEqual(bit(mask, index(8, 1, 1, "S", "ADJACENT_PAWN_SURVIVES")), 0)
        self.assertEqual(bit(mask, index(8, 1, 1, "S", "KNIGHT")), 1)


class OracleCliTests(unittest.TestCase):
    def test_capabilities_are_canonical_and_forbid_dataset_inputs(self) -> None:
        completed = run_cli("capabilities")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(completed.stdout.encode(), canonical(payload))
        self.assertFalse(payload["dataset_inputs_accepted"])
        self.assertTrue(payload["stdlib_only"])

    def test_generate_is_deterministic_with_unicode_paths(self) -> None:
        with tempfile.TemporaryDirectory() as first, tempfile.TemporaryDirectory() as second:
            first_root = Path(first) / "máscaras"
            second_root = Path(second) / "máscaras"
            first_root.mkdir()
            second_root.mkdir()
            first_output = first_root / "atomic-v3.atmask"
            first_manifest = first_root / "atomic-v3.manifest.json"
            second_output = second_root / "atomic-v3.atmask"
            second_manifest = second_root / "atomic-v3.manifest.json"
            for output, manifest in (
                (first_output, first_manifest),
                (second_output, second_manifest),
            ):
                completed = run_cli(
                    "generate",
                    "--feature-schema",
                    str(FEATURE_SCHEMA),
                    "--output",
                    str(output),
                    "--manifest",
                    str(manifest),
                )
                self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(first_output.read_bytes(), second_output.read_bytes())
            self.assertEqual(first_manifest.read_bytes(), second_manifest.read_bytes())

    def test_generate_rejects_schema_mutation_without_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            changed = root / FEATURE_SCHEMA.name
            changed.write_bytes(FEATURE_SCHEMA.read_bytes() + b"\n")
            output = root / "mask.atmask"
            manifest = root / "manifest.json"
            completed = run_cli(
                "generate",
                "--feature-schema",
                str(changed),
                "--output",
                str(output),
                "--manifest",
                str(manifest),
            )
            self.assertEqual(completed.returncode, 1)
            self.assertIn("SHA-256 mismatch", completed.stderr)
            self.assertFalse(output.exists())
            self.assertFalse(manifest.exists())

    def test_generate_rejects_duplicate_json_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            changed = root / FEATURE_SCHEMA.name
            changed.write_bytes(b'{"schema_version":1,"schema_version":1}\n')
            completed = run_cli(
                "generate",
                "--feature-schema",
                str(changed),
                "--output",
                str(root / "mask.atmask"),
                "--manifest",
                str(root / "manifest.json"),
            )
            self.assertEqual(completed.returncode, 1)
            self.assertIn("duplicate JSON key", completed.stderr)

    def test_generate_refuses_overwrite_and_leaves_existing_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "mask.atmask"
            manifest = root / "manifest.json"
            output.write_bytes(b"owner-data")
            completed = run_cli(
                "generate",
                "--feature-schema",
                str(FEATURE_SCHEMA),
                "--output",
                str(output),
                "--manifest",
                str(manifest),
            )
            self.assertEqual(completed.returncode, 1)
            self.assertIn("refusing to overwrite", completed.stderr)
            self.assertEqual(output.read_bytes(), b"owner-data")
            self.assertFalse(manifest.exists())

    def test_dataset_argument_is_not_accepted(self) -> None:
        completed = run_cli(
            "generate",
            "--feature-schema",
            str(FEATURE_SCHEMA),
            "--output",
            "unused.atmask",
            "--manifest",
            "unused.json",
            "--dataset",
            "forbidden.atbin",
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("unrecognized arguments", completed.stderr)

    def test_symlink_feature_input_is_rejected_when_supported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            link = root / "feature.json"
            try:
                os.symlink(FEATURE_SCHEMA, link)
            except (OSError, NotImplementedError) as exc:
                self.skipTest("platform cannot create test symlink: " + str(exc))
            completed = run_cli(
                "generate",
                "--feature-schema",
                str(link),
                "--output",
                str(root / "mask.atmask"),
                "--manifest",
                str(root / "manifest.json"),
            )
            self.assertEqual(completed.returncode, 1)
            self.assertIn("symbolic links", completed.stderr)

    def test_symlink_output_parent_is_rejected_when_supported(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real = root / "real"
            real.mkdir()
            linked = root / "linked"
            try:
                os.symlink(real, linked, target_is_directory=True)
            except (OSError, NotImplementedError) as exc:
                self.skipTest("platform cannot create directory symlink: " + str(exc))
            completed = run_cli(
                "generate",
                "--feature-schema",
                str(FEATURE_SCHEMA),
                "--output",
                str(linked / "mask.atmask"),
                "--manifest",
                str(linked / "manifest.json"),
            )
            self.assertEqual(completed.returncode, 1)
            self.assertIn("symbolic links", completed.stderr)
            self.assertEqual(list(real.iterdir()), [])

    def test_same_handle_change_is_rejected(self) -> None:
        original = ORACLE.os.fstat
        calls = 0

        def changed(descriptor: int) -> object:
            nonlocal calls
            calls += 1
            result = original(descriptor)
            if calls != 2:
                return result
            return types.SimpleNamespace(
                st_mode=result.st_mode,
                st_dev=result.st_dev,
                st_ino=result.st_ino,
                st_size=result.st_size + 1,
                st_mtime=result.st_mtime,
                st_ctime=result.st_ctime,
                st_mtime_ns=result.st_mtime_ns,
                st_ctime_ns=result.st_ctime_ns,
            )

        with mock.patch.object(ORACLE.os, "fstat", side_effect=changed):
            with self.assertRaisesRegex(ORACLE.OracleError, "changed during"):
                ORACLE._read_regular_snapshot(FEATURE_SCHEMA, 1_000_000, "feature")

    def test_destination_race_rolls_back_only_owned_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "mask.atmask"
            manifest = root / "manifest.json"
            original = ORACLE._link_no_replace
            calls = 0

            def racing(source: Path, destination: Path) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    destination.write_bytes(b"racer")
                original(source, destination)

            with mock.patch.object(ORACLE, "_link_no_replace", side_effect=racing):
                with self.assertRaisesRegex(ORACLE.OracleError, "destination appeared"):
                    ORACLE._publish_pair(output, b"mask", manifest, b"manifest")
            self.assertFalse(output.exists())
            self.assertEqual(manifest.read_bytes(), b"racer")
            self.assertEqual(
                [path.name for path in root.iterdir()], ["manifest.json"]
            )

    def test_second_temporary_failure_cleans_first_temporary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "mask.atmask"
            manifest = root / "manifest.json"
            original = ORACLE._write_temp
            calls = 0

            def failing(parent: Path, basename: str, payload: bytes) -> Path:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("second temp")
                return original(parent, basename, payload)

            with mock.patch.object(ORACLE, "_write_temp", side_effect=failing):
                with self.assertRaisesRegex(OSError, "second temp"):
                    ORACLE._publish_pair(output, b"mask", manifest, b"manifest")
            self.assertEqual(list(root.iterdir()), [])

    def test_single_file_fsync_failure_rolls_back(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "attestation.json"
            with mock.patch.object(ORACLE, "_fsync_directory", side_effect=OSError("fsync")):
                with self.assertRaisesRegex(OSError, "fsync"):
                    ORACLE._publish_one(output, b"evidence", "attestation")
            self.assertFalse(output.exists())


class AttestationTests(unittest.TestCase):
    def materialize(self, root: Path) -> dict[str, Path | dict | str]:
        result = ORACLE.generate_oracle_result(FEATURE_SCHEMA)
        output = root / "reachability.atmask"
        output.write_bytes(result.output)
        manifest = root / "reachability.manifest.json"
        manifest.write_bytes(canonical(dict(ORACLE.build_manifest(result, output.name))))
        policy_value = {
            "reachability_mask_sha256": result.aggregate_sha256,
            "reachability_masks": digest_map(result),
        }
        policy = root / "coverage-policy.json"
        policy.write_bytes(canonical(policy_value))
        binary_snapshot = ORACLE._read_regular_snapshot(
            SCRIPT, ORACLE.MAX_JSON_BYTES, "oracle_binary"
        )
        policy_sha = hashlib.sha256(policy.read_bytes()).hexdigest()
        controller_value = {
            "schema_version": 1,
            "schema_id": ORACLE.CONTROLLER_SCHEMA_ID,
            "campaign": {
                "file": "campaign.json",
                "bytes": "101",
                "sha256": "11" * 32,
                "schema_sha256": ORACLE.CAMPAIGN_SCHEMA_SHA256,
            },
            "producer_attestation": {
                "file": "producer.json",
                "bytes": "202",
                "sha256": "22" * 32,
                "schema_sha256": ORACLE.PRODUCER_SCHEMA_SHA256,
            },
            "coverage_policy": {
                "file": policy.name,
                "bytes": str(policy.stat().st_size),
                "sha256": policy_sha,
                "schema_sha256": ORACLE.COVERAGE_POLICY_SCHEMA_SHA256,
            },
            "oracle": {
                "commit": "33" * 20,
                "binary": {
                    "file": SCRIPT.name,
                    "bytes": str(binary_snapshot.byte_count),
                    "sha256": binary_snapshot.sha256,
                },
            },
            "verification": {
                field: True for field in ORACLE.CONTROLLER_VERIFICATION_FIELDS
            },
        }
        controller = root / "controller.json"
        controller.write_bytes(canonical(controller_value))
        return {
            "result": result,
            "output": output,
            "manifest": manifest,
            "policy": policy,
            "controller": controller,
            "controller_sha256": hashlib.sha256(controller.read_bytes()).hexdigest(),
            "controller_value": controller_value,
            "attestation": root / "reachability-attestation.json",
        }

    def command(self, paths: dict[str, Path | dict | str]) -> list[str]:
        return [
            "attest",
            "--feature-schema",
            str(FEATURE_SCHEMA),
            "--oracle-output",
            str(paths["output"]),
            "--manifest",
            str(paths["manifest"]),
            "--controller-descriptors",
            str(paths["controller"]),
            "--expected-controller-sha256",
            str(paths["controller_sha256"]),
            "--coverage-policy",
            str(paths["policy"]),
            "--oracle-binary",
            str(SCRIPT),
            "--attestation",
            str(paths["attestation"]),
        ]

    def test_attestation_matches_frozen_h9_3l_a_shape_and_hash_formula(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = self.materialize(Path(directory))
            completed = run_cli(*self.command(paths))
            self.assertEqual(completed.returncode, 0, completed.stderr)
            attestation_path = paths["attestation"]
            assert isinstance(attestation_path, Path)
            payload = attestation_path.read_bytes()
            attestation = json.loads(payload)
            self.assertEqual(payload, canonical(attestation))

            schema = json.loads(ATTESTATION_SCHEMA.read_text(encoding="utf-8"))
            self.assertEqual(tuple(attestation), tuple(schema["required"]))
            self.assertEqual(
                tuple(attestation["oracle"]), tuple(schema["$defs"]["oracle"]["required"])
            )
            self.assertEqual(
                tuple(attestation["verification"]),
                tuple(schema["$defs"]["verification"]["required"]),
            )
            self.assertEqual(attestation["oracle_output"]["bytes"], "18772")
            self.assertEqual(
                attestation["evidence_sha256"],
                ORACLE._attestation_evidence_sha256(attestation),
            )

    def test_attest_requires_explicit_controller_descriptors(self) -> None:
        completed = run_cli(
            "attest",
            "--feature-schema",
            str(FEATURE_SCHEMA),
            "--oracle-output",
            "missing.atmask",
            "--manifest",
            "missing.json",
            "--coverage-policy",
            "missing-policy.json",
            "--oracle-binary",
            str(SCRIPT),
            "--attestation",
            "attestation.json",
        )
        self.assertEqual(completed.returncode, 2)
        self.assertIn("--controller-descriptors", completed.stderr)

    def test_attest_requires_exact_external_controller_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = self.materialize(Path(directory))
            paths["controller_sha256"] = "00" * 32
            completed = run_cli(*self.command(paths))
            self.assertEqual(completed.returncode, 1)
            self.assertIn("external controller trust anchor", completed.stderr)
            self.assertFalse(Path(paths["attestation"]).exists())

    def test_attest_rejects_untrusted_or_mismatched_controller_claims(self) -> None:
        mutations = (
            ("campaign schema", lambda value: value["campaign"].__setitem__("schema_sha256", "00" * 32)),
            ("producer claim", lambda value: value["verification"].__setitem__("producer_attestation_authenticated", False)),
            ("binary digest", lambda value: value["oracle"]["binary"].__setitem__("sha256", "00" * 32)),
        )
        for label, mutate in mutations:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as directory:
                paths = self.materialize(Path(directory))
                controller_value = copy.deepcopy(paths["controller_value"])
                assert isinstance(controller_value, dict)
                mutate(controller_value)
                controller = paths["controller"]
                assert isinstance(controller, Path)
                controller.write_bytes(canonical(controller_value))
                paths["controller_sha256"] = hashlib.sha256(
                    controller.read_bytes()
                ).hexdigest()
                completed = run_cli(*self.command(paths))
                self.assertEqual(completed.returncode, 1)
                self.assertFalse(Path(paths["attestation"]).exists())

    def test_attest_rejects_policy_or_oracle_output_mutation(self) -> None:
        for mutation in ("policy", "oracle"):
            with self.subTest(mutation=mutation), tempfile.TemporaryDirectory() as directory:
                paths = self.materialize(Path(directory))
                if mutation == "policy":
                    policy = Path(paths["policy"])
                    value = json.loads(policy.read_text(encoding="utf-8"))
                    value["reachability_mask_sha256"] = "00" * 32
                    policy.write_bytes(canonical(value))
                    controller_value = copy.deepcopy(paths["controller_value"])
                    assert isinstance(controller_value, dict)
                    controller_value["coverage_policy"]["bytes"] = str(policy.stat().st_size)
                    controller_value["coverage_policy"]["sha256"] = hashlib.sha256(
                        policy.read_bytes()
                    ).hexdigest()
                    controller = Path(paths["controller"])
                    controller.write_bytes(canonical(controller_value))
                    paths["controller_sha256"] = hashlib.sha256(
                        controller.read_bytes()
                    ).hexdigest()
                else:
                    output = Path(paths["output"])
                    payload = bytearray(output.read_bytes())
                    payload[0] ^= 1
                    output.write_bytes(payload)
                completed = run_cli(*self.command(paths))
                self.assertEqual(completed.returncode, 1)
                self.assertFalse(Path(paths["attestation"]).exists())

    def test_attest_refuses_existing_or_symlink_destination(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self.materialize(root)
            destination = Path(paths["attestation"])
            destination.write_bytes(b"owner")
            completed = run_cli(*self.command(paths))
            self.assertEqual(completed.returncode, 1)
            self.assertEqual(destination.read_bytes(), b"owner")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = self.materialize(root)
            destination = Path(paths["attestation"])
            target = root / "target.json"
            try:
                os.symlink(target, destination)
            except (OSError, NotImplementedError) as exc:
                self.skipTest("platform cannot create output symlink: " + str(exc))
            completed = run_cli(*self.command(paths))
            self.assertEqual(completed.returncode, 1)
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
