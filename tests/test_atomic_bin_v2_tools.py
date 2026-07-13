#!/usr/bin/env python3
"""Unit tests for the authenticated Atomic BIN V2 launcher."""

from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "atomic_bin_v2_tools", ROOT / "script" / "atomic_bin_v2_tools.py"
)
assert SPEC is not None and SPEC.loader is not None
launcher = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(launcher)

DATA_SHA = "0352b036f2a140c609e3eb9c9d635dc553e8d77253d8faa92437390f5cf93cb6"
MANIFEST_SHA = "83d63922df3ac4a0c81a21ec9d9fd9e180efe50f26efee62fe01710e09da5b42"
DECODE_SHA = "5e3f8d7c6db6ee955b71747ee063859e15609adb557a3754228a606f3df2caad"
CAPABILITIES = (
    '{"type":"atomic-data-tools-capabilities","contract_version":1,'
    '"formats":{"atomic-bin-v2":{"data_schema_sha256":"'
    + DATA_SHA
    + '","manifest_schema_sha256":"'
    + MANIFEST_SHA
    + '","decode_schema_sha256":"'
    + DECODE_SHA
    + '","entrypoint":"manifest","read":true,"write":false,'
    '"operations":["validate","decode"]}}}\n'
)

FAKE_CHILD = r'''#!/usr/bin/env python3
import json
import os
from pathlib import Path
import sys

log = Path(os.environ["ATOMIC_LAUNCHER_TEST_LOG"])
with log.open("a", encoding="utf-8", newline="\n") as target:
    target.write(json.dumps(sys.argv[1:], ensure_ascii=False, separators=(",", ":")) + "\n")

if sys.argv[1:] == ["capabilities"]:
    sys.stdout.buffer.write(bytes.fromhex(os.environ["ATOMIC_LAUNCHER_TEST_CAPS_HEX"]))
    sys.stderr.buffer.write(bytes.fromhex(os.environ.get("ATOMIC_LAUNCHER_TEST_CAPS_ERR_HEX", "")))
    raise SystemExit(int(os.environ.get("ATOMIC_LAUNCHER_TEST_CAPS_EXIT", "0")))

sys.stdout.buffer.write(bytes.fromhex(os.environ.get("ATOMIC_LAUNCHER_TEST_STDOUT_HEX", "")))
sys.stderr.buffer.write(bytes.fromhex(os.environ.get("ATOMIC_LAUNCHER_TEST_STDERR_HEX", "")))
raise SystemExit(int(os.environ.get("ATOMIC_LAUNCHER_TEST_EXIT", "0")))
'''


class AtomicBinV2LauncherTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="atomic-v2-launcher-")
        self.root = Path(self.temporary.name)
        self.lock = self.root / "atomic-engine.lock.json"
        self.child = self.root / "fake child é.py"
        self.pin = self.root / "verify source pin.py"
        self.log = self.root / "child argv.jsonl"
        self.child.write_text(FAKE_CHILD, encoding="utf-8", newline="\n")
        self.pin.write_text(
            "raise SystemExit(0)\n", encoding="utf-8", newline="\n"
        )
        self.lock.write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "data_tools_contract": {
                        "contract_version": 1,
                        "data_schema": {
                            "path": "schemas/atomic-bin-v2.json",
                            "sha256": DATA_SHA,
                        },
                        "manifest_schema": {
                            "path": "schemas/atomic-bin-v2-manifest.json",
                            "sha256": MANIFEST_SHA,
                        },
                        "decode_schema": {
                            "path": "schemas/atomic-data-tools-decode-v1.json",
                            "sha256": DECODE_SHA,
                        },
                        "capabilities": CAPABILITIES,
                    }
                },
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
            newline="\n",
        )
        self.old_environment = os.environ.copy()
        os.environ.update(
            {
                "ATOMIC_LAUNCHER_TEST_LOG": str(self.log),
                "ATOMIC_LAUNCHER_TEST_CAPS_HEX": CAPABILITIES.encode("utf-8").hex(),
                "ATOMIC_LAUNCHER_TEST_CAPS_ERR_HEX": "",
                "ATOMIC_LAUNCHER_TEST_CAPS_EXIT": "0",
                "ATOMIC_LAUNCHER_TEST_STDOUT_HEX": "",
                "ATOMIC_LAUNCHER_TEST_STDERR_HEX": "",
                "ATOMIC_LAUNCHER_TEST_EXIT": "0",
            }
        )

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self.old_environment)
        self.temporary.cleanup()

    def invoke(self, arguments: list[str]) -> tuple[int, bytes, bytes]:
        stdout = io.BytesIO()
        stderr = io.BytesIO()
        code = launcher.main(
            arguments,
            _child_command=(sys.executable, str(self.child)),
            _pin_command=(sys.executable, str(self.pin)),
            _lock_path=self.lock,
            _stdout=stdout,
            _stderr=stderr,
        )
        return code, stdout.getvalue(), stderr.getvalue()

    def invocations(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def test_capabilities_are_authenticated_and_relayed_exactly_once(self) -> None:
        code, stdout, stderr = self.invoke(["capabilities"])
        self.assertEqual((code, stdout, stderr), (0, CAPABILITIES.encode(), b""))
        self.assertNotIn(b"\r", stdout)
        self.assertEqual(self.invocations(), [["capabilities"]])

    def test_validate_preserves_order_unicode_spaces_and_output_bytes(self) -> None:
        manifest = str(self.root / "jeu é atomique.atbin.manifest.json")
        arguments = [
            "validate",
            "--manifest",
            manifest,
            "--format",
            "atomic-bin-v2",
        ]
        expected = '{"ok":"\u00e9"}\n'.encode("utf-8")
        os.environ["ATOMIC_LAUNCHER_TEST_STDOUT_HEX"] = expected.hex()
        code, stdout, stderr = self.invoke(arguments)
        self.assertEqual(code, 0)
        self.assertEqual(stdout, expected)
        self.assertEqual(stderr, b"")
        self.assertEqual(self.invocations(), [["capabilities"], arguments])

    def test_validate_preserves_each_contract_exit_and_both_streams(self) -> None:
        arguments = [
            "validate",
            "--format",
            "atomic-bin-v2",
            "--manifest",
            "dataset.atbin.manifest.json",
        ]
        for child_exit in (0, 2, 3):
            with self.subTest(child_exit=child_exit):
                self.log.unlink(missing_ok=True)
                expected_stdout = b"stdout\x00bytes\n"
                expected_stderr = b"stderr\xffbytes\n"
                os.environ["ATOMIC_LAUNCHER_TEST_STDOUT_HEX"] = expected_stdout.hex()
                os.environ["ATOMIC_LAUNCHER_TEST_STDERR_HEX"] = expected_stderr.hex()
                os.environ["ATOMIC_LAUNCHER_TEST_EXIT"] = str(child_exit)
                code, stdout, stderr = self.invoke(arguments)
                self.assertEqual(
                    (code, stdout, stderr),
                    (child_exit, expected_stdout, expected_stderr),
                )
                self.assertEqual(self.invocations(), [["capabilities"], arguments])

    def test_decode_preserves_unicode_order_and_exact_contract_results(self) -> None:
        arguments = [
            "decode",
            "--limit",
            "2",
            "--manifest",
            str(self.root / "données atomiques é.atbin.manifest.json"),
            "--offset",
            "0",
            "--format",
            "atomic-bin-v2",
        ]
        cases = (
            (
                0,
                (
                    '{"type":"atomic-data-tools-decode-header","status":"ok"}\n'
                    '{"type":"atomic-data-tools-decode-record","fen":"é"}\n'
                    '{"type":"atomic-data-tools-decode-record","fen":"ñ"}\n'
                    '{"type":"atomic-data-tools-decode-footer","status":"ok"}\n'
                ).encode("utf-8"),
                b"",
            ),
            (
                2,
                b"",
                '{"status":"error","code":"missing_value","detail":"é"}\n'.encode(),
            ),
            (
                3,
                b"",
                '{"status":"error","code":"invalid_record","detail":"ñ"}\n'.encode(),
            ),
        )
        for child_exit, expected_stdout, expected_stderr in cases:
            with self.subTest(child_exit=child_exit):
                self.log.unlink(missing_ok=True)
                os.environ["ATOMIC_LAUNCHER_TEST_STDOUT_HEX"] = expected_stdout.hex()
                os.environ["ATOMIC_LAUNCHER_TEST_STDERR_HEX"] = expected_stderr.hex()
                os.environ["ATOMIC_LAUNCHER_TEST_EXIT"] = str(child_exit)
                code, stdout, stderr = self.invoke(arguments)
                self.assertEqual(
                    (code, stdout, stderr),
                    (child_exit, expected_stdout, expected_stderr),
                )
                self.assertEqual(self.invocations(), [["capabilities"], arguments])

    def test_decode_unexpected_child_exit_fails_closed_without_output_leak(self) -> None:
        arguments = [
            "decode",
            "--format",
            "atomic-bin-v2",
            "--manifest",
            "dataset.atbin.manifest.json",
            "--limit",
            "1",
        ]
        os.environ["ATOMIC_LAUNCHER_TEST_STDOUT_HEX"] = b"must-not-leak\n".hex()
        os.environ["ATOMIC_LAUNCHER_TEST_STDERR_HEX"] = b"child-crash-detail\n".hex()
        os.environ["ATOMIC_LAUNCHER_TEST_EXIT"] = "4"
        code, stdout, stderr = self.invoke(arguments)
        self.assertEqual(code, 3)
        self.assertEqual(stdout, b"")
        self.assertEqual(
            stderr,
            b"Atomic BIN V2 launcher error: pinned child returned unsupported exit code 4\n",
        )
        self.assertEqual(self.invocations(), [["capabilities"], arguments])

    def test_decode_schema_hash_and_operation_order_are_fail_closed(self) -> None:
        original = json.loads(self.lock.read_text(encoding="utf-8"))
        cases = (
            (
                "missing decode schema",
                lambda lock: lock["data_tools_contract"].pop("decode_schema"),
                b"missing or unknown fields",
            ),
            (
                "decode schema hash drift",
                lambda lock: lock["data_tools_contract"]["decode_schema"].update(
                    {"sha256": "0" * 64}
                ),
                b"locked schemas and operations",
            ),
            (
                "operation order drift",
                lambda lock: lock["data_tools_contract"].update(
                    {
                        "capabilities": CAPABILITIES.replace(
                            '["validate","decode"]', '["decode","validate"]'
                        )
                    }
                ),
                b"locked schemas and operations",
            ),
        )
        for label, mutate, expected_error in cases:
            with self.subTest(label=label):
                self.log.unlink(missing_ok=True)
                locked = json.loads(json.dumps(original))
                mutate(locked)
                self.lock.write_text(
                    json.dumps(locked, separators=(",", ":")) + "\n",
                    encoding="utf-8",
                    newline="\n",
                )
                code, stdout, stderr = self.invoke(
                    [
                        "decode",
                        "--format",
                        "atomic-bin-v2",
                        "--manifest",
                        "dataset.atbin.manifest.json",
                        "--limit",
                        "1",
                    ]
                )
                self.assertEqual(code, 3)
                self.assertEqual(stdout, b"")
                self.assertIn(expected_error, stderr)
                self.assertEqual(self.invocations(), [])

    def test_raw_shard_positional_and_unknown_are_not_rewritten(self) -> None:
        cases = (
            ["validate", "--format", "atomic-bin-v2", "--manifest", "raw.atbin"],
            ["validate", "raw.atbin.manifest.json"],
            ["validate", "--input", "dataset.atbin.manifest.json"],
        )
        os.environ["ATOMIC_LAUNCHER_TEST_STDERR_HEX"] = b'{"code":"contract"}\n'.hex()
        os.environ["ATOMIC_LAUNCHER_TEST_EXIT"] = "2"
        for arguments in cases:
            with self.subTest(arguments=arguments):
                self.log.unlink(missing_ok=True)
                code, stdout, stderr = self.invoke(arguments)
                self.assertEqual((code, stdout, stderr), (2, b"", b'{"code":"contract"}\n'))
                self.assertEqual(self.invocations(), [["capabilities"], arguments])

    def test_shell_metacharacters_are_one_literal_argument(self) -> None:
        marker = self.root / "must-not-exist"
        manifest = (
            f'$(touch "{marker}") & type nul > "{marker}" & '
            "dataset.atbin.manifest.json"
        )
        arguments = [
            "validate",
            "--format",
            "atomic-bin-v2",
            "--manifest",
            manifest,
        ]
        code, _, _ = self.invoke(arguments)
        self.assertEqual(code, 0)
        self.assertFalse(marker.exists())
        self.assertEqual(self.invocations(), [["capabilities"], arguments])

    def test_capability_mismatch_fails_closed_before_requested_command(self) -> None:
        os.environ["ATOMIC_LAUNCHER_TEST_CAPS_HEX"] = b"{}\n".hex()
        code, stdout, stderr = self.invoke(
            [
                "validate",
                "--format",
                "atomic-bin-v2",
                "--manifest",
                "dataset.atbin.manifest.json",
            ]
        )
        self.assertEqual(code, 3)
        self.assertEqual(stdout, b"")
        self.assertIn(b"capabilities do not match", stderr)
        self.assertEqual(self.invocations(), [["capabilities"]])

    def test_source_pin_failure_prevents_child_execution(self) -> None:
        self.pin.write_text(
            'print("gitlink mismatch")\nraise SystemExit(1)\n',
            encoding="utf-8",
            newline="\n",
        )
        code, stdout, stderr = self.invoke(
            [
                "validate",
                "--format",
                "atomic-bin-v2",
                "--manifest",
                "dataset.atbin.manifest.json",
            ]
        )
        self.assertEqual(code, 3)
        self.assertEqual(stdout, b"")
        self.assertIn(b"source-pin verification failed", stderr)
        self.assertIn(b"gitlink mismatch", stderr)
        self.assertEqual(self.invocations(), [])

    def test_old_or_malformed_lock_fails_before_child_invocation(self) -> None:
        locked = json.loads(self.lock.read_text(encoding="utf-8"))
        locked["schema_version"] = 1
        self.lock.write_text(json.dumps(locked), encoding="utf-8", newline="\n")
        code, stdout, stderr = self.invoke(["capabilities"])
        self.assertEqual(code, 3)
        self.assertEqual(stdout, b"")
        self.assertIn(b"schema_version", stderr)
        self.assertEqual(self.invocations(), [])

        locked["schema_version"] = 2
        locked["data_tools_contract"]["contract_version"] = True
        self.lock.write_text(json.dumps(locked), encoding="utf-8", newline="\n")
        code, stdout, stderr = self.invoke(["capabilities"])
        self.assertEqual(code, 3)
        self.assertEqual(stdout, b"")
        self.assertIn(b"contract version", stderr)
        self.assertEqual(self.invocations(), [])

        self.lock.write_bytes(b"{not-json\n")
        code, stdout, stderr = self.invoke(["capabilities"])
        self.assertEqual(code, 3)
        self.assertEqual(stdout, b"")
        self.assertIn(b"cannot read Atomic engine lock", stderr)
        self.assertEqual(self.invocations(), [])

    def test_missing_child_executable_is_a_fail_closed_error(self) -> None:
        stdout = io.BytesIO()
        stderr = io.BytesIO()
        missing = self.root / "missing atomic-stockfish-data-tools"
        code = launcher.main(
            [
                "validate",
                "--format",
                "atomic-bin-v2",
                "--manifest",
                "dataset.atbin.manifest.json",
            ],
            _child_command=(str(missing),),
            _pin_command=(sys.executable, str(self.pin)),
            _lock_path=self.lock,
            _stdout=stdout,
            _stderr=stderr,
        )
        self.assertEqual(code, 3)
        self.assertEqual(stdout.getvalue(), b"")
        self.assertIn(b"cannot execute pinned Atomic data-tools", stderr.getvalue())
        self.assertEqual(self.invocations(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
