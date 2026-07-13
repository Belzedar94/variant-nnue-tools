#!/usr/bin/env python3
"""Negative and positive tests for the Atomic engine submodule lock."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import atomic_engine_pin


CANONICAL_URL = "https://github.com/Belzedar94/Atomic-Stockfish.git"


def git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        [atomic_engine_pin.GIT, "-C", str(root), *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        raise AssertionError(completed.stderr)
    return completed.stdout.strip()


def write(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8", newline="\n")


class AtomicEnginePinTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="atomic-engine-pin-")
        self.root = Path(self.temporary.name)
        self.engine_source = self.root / "engine-source"
        self.superproject = self.root / "tools"

        self.engine_source.mkdir()
        git(self.engine_source, "init")
        git(self.engine_source, "config", "user.email", "atomic-pin@example.invalid")
        git(self.engine_source, "config", "user.name", "Atomic Pin Test")
        schema = {
            "schema_id": "legacy-atomic-v1",
            "variant": "atomic",
            "format": {"record_size": 72, "atomic960": False},
        }
        write(
            self.engine_source / "schemas" / "atomic-schema.json",
            json.dumps(schema, sort_keys=True, separators=(",", ":")) + "\n",
        )
        for relative_path in (
            atomic_engine_pin.V2_DATA_SCHEMA_PATH,
            atomic_engine_pin.V2_MANIFEST_SCHEMA_PATH,
        ):
            source = (
                atomic_engine_pin.REPO_ROOT
                / "engine"
                / "Atomic-Stockfish"
                / relative_path
            )
            self.assertTrue(source.is_file(), f"missing pinned fixture {source}")
            destination = self.engine_source / relative_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source.read_bytes())
        write(self.engine_source / ".gitattributes", "schemas/*.json text eol=lf\n")
        write(
            self.engine_source / "src" / "Makefile",
            "build: config-sanity\n\n"
            "data-generator: config-sanity\n\n"
            "data-tools: config-sanity\n",
        )
        git(self.engine_source, "add", ".")
        git(self.engine_source, "commit", "-m", "fixture")
        self.commit = git(self.engine_source, "rev-parse", "HEAD")

        self.superproject.mkdir()
        git(self.superproject, "init")
        git(self.superproject, "config", "user.email", "atomic-pin@example.invalid")
        git(self.superproject, "config", "user.name", "Atomic Pin Test")
        subprocess.run(
            [
                atomic_engine_pin.GIT,
                "-c",
                "protocol.file.allow=always",
                "-C",
                str(self.superproject),
                "submodule",
                "add",
                str(self.engine_source),
                "engine/Atomic-Stockfish",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
        git(
            self.superproject / "engine" / "Atomic-Stockfish",
            "remote",
            "set-url",
            "origin",
            CANONICAL_URL,
        )
        git(
            self.superproject / "engine" / "Atomic-Stockfish",
            "update-ref",
            "refs/remotes/origin/main",
            self.commit,
        )
        git(
            self.superproject,
            "config",
            "-f",
            ".gitmodules",
            "submodule.engine/Atomic-Stockfish.url",
            CANONICAL_URL,
        )
        git(self.superproject, "add", ".gitmodules", "engine/Atomic-Stockfish")
        git(self.superproject, "commit", "-m", "pin engine")

        schema_path = (
            self.superproject
            / "engine"
            / "Atomic-Stockfish"
            / "schemas"
            / "atomic-schema.json"
        )
        self.lock = {
            "schema_version": 2,
            "submodule": {
                "name": "engine/Atomic-Stockfish",
                "path": "engine/Atomic-Stockfish",
                "url": CANONICAL_URL,
                "commit": self.commit,
                "required_ref": "refs/remotes/origin/main",
            },
            "data_schema": {
                "path": "schemas/atomic-schema.json",
                "sha256": hashlib.sha256(schema_path.read_bytes()).hexdigest(),
                "schema_id": "legacy-atomic-v1",
                "variant": "atomic",
                "record_size": 72,
                "atomic960": False,
            },
            "build_contract": {
                "playing_target": "build",
                "data_generator_target": "data-generator",
                "data_tools_target": "data-tools",
                "playing_artifacts": {
                    "linux": "src/atomic-stockfish",
                    "windows": "src/atomic-stockfish.exe",
                },
                "data_generator_artifacts": {
                    "linux": "src/atomic-stockfish-data-generator",
                    "windows": "src/atomic-stockfish-data-generator.exe",
                },
                "data_tools_artifacts": {
                    "linux": "src/atomic-stockfish-data-tools",
                    "windows": "src/atomic-stockfish-data-tools.exe",
                },
            },
            "data_tools_contract": {
                "contract_version": 1,
                "data_schema": {
                    "path": atomic_engine_pin.V2_DATA_SCHEMA_PATH,
                    "sha256": atomic_engine_pin.V2_DATA_SCHEMA_SHA256,
                },
                "manifest_schema": {
                    "path": atomic_engine_pin.V2_MANIFEST_SCHEMA_PATH,
                    "sha256": atomic_engine_pin.V2_MANIFEST_SCHEMA_SHA256,
                },
                "capabilities": atomic_engine_pin.DATA_TOOLS_CAPABILITIES,
            },
        }
        self.lock_path = self.superproject / "atomic-engine.lock.json"
        self.write_lock()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_lock(self, *, stage: bool = True) -> None:
        write(self.lock_path, json.dumps(self.lock, indent=2) + "\n")
        if stage:
            git(self.superproject, "add", self.lock_path.name)

    def commit_engine_schema_drift(self, relative_path: str) -> None:
        submodule = self.superproject / "engine" / "Atomic-Stockfish"
        git(submodule, "config", "user.email", "atomic-pin@example.invalid")
        git(submodule, "config", "user.name", "Atomic Pin Test")
        schema_path = submodule / relative_path
        schema_path.write_bytes(schema_path.read_bytes() + b" ")
        git(submodule, "add", relative_path)
        git(submodule, "commit", "-m", "mutate authenticated schema")
        drift_commit = git(submodule, "rev-parse", "HEAD")
        git(submodule, "update-ref", "refs/remotes/origin/main", drift_commit)
        self.lock["submodule"]["commit"] = drift_commit
        self.write_lock()
        git(self.superproject, "add", "engine/Atomic-Stockfish")

    def test_exact_clean_pin_passes(self) -> None:
        verified = atomic_engine_pin.verify_engine_pin(self.superproject, self.lock_path)
        self.assertEqual(verified["data_schema"]["schema_id"], "legacy-atomic-v1")
        self.assertEqual(
            verified["data_tools_contract"]["capabilities"],
            atomic_engine_pin.DATA_TOOLS_CAPABILITIES,
        )
        self.assertEqual(
            json.dumps(
                json.loads(verified["data_tools_contract"]["capabilities"]),
                ensure_ascii=False,
                separators=(",", ":"),
            )
            + "\n",
            verified["data_tools_contract"]["capabilities"],
        )

    def test_noncanonical_or_windows_style_paths_are_rejected(self) -> None:
        self.assertEqual(
            atomic_engine_pin.safe_relative_path(
                "schemas/atomic-bin-v2.json", "path"
            ).as_posix(),
            "schemas/atomic-bin-v2.json",
        )
        for raw in (
            r"C:\evil",
            r"..\evil",
            r"\\server\share",
            "C:/evil",
            "../evil",
            "/absolute",
            "./schemas/schema.json",
            "schemas//schema.json",
            "schemas/../schema.json",
            "schemas/schema.json:stream",
        ):
            with self.subTest(raw=raw):
                with self.assertRaisesRegex(
                    atomic_engine_pin.PinError,
                    "canonical repository-relative POSIX path",
                ):
                    atomic_engine_pin.safe_relative_path(raw, "path")

    def test_data_tools_contract_mutations_are_rejected(self) -> None:
        original = copy.deepcopy(self.lock)
        cases = (
            (
                "target",
                ("build_contract", "data_tools_target"),
                "wrong-target",
                "data-tools target",
            ),
            (
                "artifact",
                ("build_contract", "data_tools_artifacts", "linux"),
                "src/wrong-data-tools",
                "data_tools_artifacts",
            ),
            (
                "version",
                ("data_tools_contract", "contract_version"),
                2,
                "contract_version",
            ),
            (
                "boolean version",
                ("data_tools_contract", "contract_version"),
                True,
                "contract_version",
            ),
            (
                "data schema path",
                ("data_tools_contract", "data_schema", "path"),
                "schemas/wrong.json",
                "data_schema path",
            ),
            (
                "data schema hash",
                ("data_tools_contract", "data_schema", "sha256"),
                "0" * 64,
                "data_schema SHA-256",
            ),
            (
                "manifest schema path",
                ("data_tools_contract", "manifest_schema", "path"),
                "schemas/wrong-manifest.json",
                "manifest_schema path",
            ),
            (
                "manifest schema hash",
                ("data_tools_contract", "manifest_schema", "sha256"),
                "0" * 64,
                "manifest_schema SHA-256",
            ),
            (
                "capabilities missing LF",
                ("data_tools_contract", "capabilities"),
                atomic_engine_pin.DATA_TOOLS_CAPABILITIES.rstrip("\n"),
                "LF-terminated",
            ),
            (
                "capabilities noncanonical JSON",
                ("data_tools_contract", "capabilities"),
                atomic_engine_pin.DATA_TOOLS_CAPABILITIES.replace(
                    '":"atomic-data-tools-capabilities"',
                    '": "atomic-data-tools-capabilities"',
                    1,
                ),
                "canonical minified JSON",
            ),
            (
                "capabilities semantic drift",
                ("data_tools_contract", "capabilities"),
                atomic_engine_pin.DATA_TOOLS_CAPABILITIES.replace(
                    '"operations":["validate"]',
                    '"operations":[]',
                    1,
                ),
                "capabilities contract",
            ),
        )

        for label, path, value, message in cases:
            with self.subTest(label=label):
                self.lock = copy.deepcopy(original)
                destination = self.lock
                for component in path[:-1]:
                    destination = destination[component]
                destination[path[-1]] = value
                self.write_lock()
                with self.assertRaisesRegex(atomic_engine_pin.PinError, message):
                    atomic_engine_pin.verify_engine_pin(self.superproject, self.lock_path)

        self.lock = original
        self.write_lock()

    def test_legacy_lock_schema_version_is_rejected(self) -> None:
        self.lock["schema_version"] = 1
        self.write_lock()
        with self.assertRaisesRegex(atomic_engine_pin.PinError, "schema_version"):
            atomic_engine_pin.verify_engine_pin(self.superproject, self.lock_path)

    def test_wrong_commit_is_rejected(self) -> None:
        self.lock["submodule"]["commit"] = "0" * 40
        self.write_lock()
        with self.assertRaisesRegex(atomic_engine_pin.PinError, "gitlink"):
            atomic_engine_pin.verify_engine_pin(self.superproject, self.lock_path)

    def test_uppercase_commit_is_rejected(self) -> None:
        self.lock["submodule"]["commit"] = self.commit.upper()
        self.write_lock()
        with self.assertRaisesRegex(atomic_engine_pin.PinError, "lowercase"):
            atomic_engine_pin.verify_engine_pin(self.superproject, self.lock_path)

    def test_checked_out_commit_drift_is_rejected(self) -> None:
        submodule = self.superproject / "engine" / "Atomic-Stockfish"
        git(submodule, "config", "user.email", "atomic-pin@example.invalid")
        git(submodule, "config", "user.name", "Atomic Pin Test")
        write(submodule / "drift.txt", "drift\n")
        git(submodule, "add", "drift.txt")
        git(submodule, "commit", "-m", "drift")
        with self.assertRaisesRegex(atomic_engine_pin.PinError, "checked-out"):
            atomic_engine_pin.verify_engine_pin(self.superproject, self.lock_path)

    def test_dirty_submodule_is_rejected(self) -> None:
        write(self.superproject / "engine" / "Atomic-Stockfish" / "dirty.txt", "dirty\n")
        with self.assertRaisesRegex(atomic_engine_pin.PinError, "dirty"):
            atomic_engine_pin.verify_engine_pin(self.superproject, self.lock_path)

    def test_wrong_schema_hash_is_rejected(self) -> None:
        self.lock["data_schema"]["sha256"] = "0" * 64
        self.write_lock()
        with self.assertRaisesRegex(atomic_engine_pin.PinError, "SHA-256"):
            atomic_engine_pin.verify_engine_pin(self.superproject, self.lock_path)

    def test_authenticated_v2_data_schema_mutation_is_rejected(self) -> None:
        self.commit_engine_schema_drift(atomic_engine_pin.V2_DATA_SCHEMA_PATH)
        with self.assertRaisesRegex(
            atomic_engine_pin.PinError, "data_schema SHA-256 mismatch"
        ):
            atomic_engine_pin.verify_engine_pin(self.superproject, self.lock_path)

    def test_authenticated_v2_manifest_schema_mutation_is_rejected(self) -> None:
        self.commit_engine_schema_drift(atomic_engine_pin.V2_MANIFEST_SCHEMA_PATH)
        with self.assertRaisesRegex(
            atomic_engine_pin.PinError, "manifest_schema SHA-256 mismatch"
        ):
            atomic_engine_pin.verify_engine_pin(self.superproject, self.lock_path)

    def test_wrong_gitmodules_url_is_rejected(self) -> None:
        git(
            self.superproject,
            "config",
            "-f",
            ".gitmodules",
            "submodule.engine/Atomic-Stockfish.url",
            "https://example.invalid/wrong.git",
        )
        git(self.superproject, "add", ".gitmodules")
        with self.assertRaisesRegex(atomic_engine_pin.PinError, "URL"):
            atomic_engine_pin.verify_engine_pin(self.superproject, self.lock_path)

    def test_missing_submodule_is_rejected(self) -> None:
        submodule = self.superproject / "engine" / "Atomic-Stockfish"
        submodule.rename(self.root / "removed-submodule")
        with self.assertRaisesRegex(atomic_engine_pin.PinError, "missing"):
            atomic_engine_pin.verify_engine_pin(self.superproject, self.lock_path)

    def test_non_gitlink_is_rejected(self) -> None:
        git(
            self.superproject,
            "rm",
            "--cached",
            "-f",
            "engine/Atomic-Stockfish",
        )
        with self.assertRaisesRegex(atomic_engine_pin.PinError, "exactly one index entry"):
            atomic_engine_pin.verify_engine_pin(self.superproject, self.lock_path)

    def test_unknown_lock_key_is_rejected(self) -> None:
        self.lock["unexpected"] = True
        self.write_lock()
        with self.assertRaisesRegex(atomic_engine_pin.PinError, "unknown"):
            atomic_engine_pin.verify_engine_pin(self.superproject, self.lock_path)

    def test_unstaged_lock_drift_is_rejected(self) -> None:
        self.lock["unexpected"] = True
        self.write_lock(stage=False)
        with self.assertRaisesRegex(atomic_engine_pin.PinError, "index snapshot"):
            atomic_engine_pin.verify_engine_pin(self.superproject, self.lock_path)

    def test_unstaged_gitmodules_drift_is_rejected(self) -> None:
        git(
            self.superproject,
            "config",
            "-f",
            ".gitmodules",
            "submodule.engine/Atomic-Stockfish.branch",
            "main",
        )
        with self.assertRaisesRegex(atomic_engine_pin.PinError, "index snapshot"):
            atomic_engine_pin.verify_engine_pin(self.superproject, self.lock_path)

    def test_staged_gitmodules_branch_is_rejected(self) -> None:
        git(
            self.superproject,
            "config",
            "-f",
            ".gitmodules",
            "submodule.engine/Atomic-Stockfish.branch",
            "main",
        )
        git(self.superproject, "add", ".gitmodules")
        with self.assertRaisesRegex(atomic_engine_pin.PinError, "unknown submodule fields"):
            atomic_engine_pin.verify_engine_pin(self.superproject, self.lock_path)

    def test_missing_required_ref_is_rejected(self) -> None:
        git(
            self.superproject / "engine" / "Atomic-Stockfish",
            "update-ref",
            "-d",
            "refs/remotes/origin/main",
        )
        with self.assertRaisesRegex(atomic_engine_pin.PinError, "required ref is missing"):
            atomic_engine_pin.verify_engine_pin(self.superproject, self.lock_path)

    def test_unmerged_engine_commit_is_rejected(self) -> None:
        submodule = self.superproject / "engine" / "Atomic-Stockfish"
        git(submodule, "config", "user.email", "atomic-pin@example.invalid")
        git(submodule, "config", "user.name", "Atomic Pin Test")
        write(submodule / "unmerged.txt", "unmerged\n")
        git(submodule, "add", "unmerged.txt")
        git(submodule, "commit", "-m", "unmerged")
        unmerged = git(submodule, "rev-parse", "HEAD")
        self.lock["submodule"]["commit"] = unmerged
        self.write_lock()
        git(self.superproject, "add", "engine/Atomic-Stockfish")
        with self.assertRaisesRegex(atomic_engine_pin.PinError, "not merged"):
            atomic_engine_pin.verify_engine_pin(self.superproject, self.lock_path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
