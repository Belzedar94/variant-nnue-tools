#!/usr/bin/env python3
"""Authenticated thin launcher for the pinned Atomic BIN V2 data tools."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import BinaryIO, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
LOCK_PATH = REPO_ROOT / "atomic-engine.lock.json"
PIN_VERIFIER = REPO_ROOT / "tests" / "atomic_engine_pin.py"
ENGINE_TOOLS = (
    REPO_ROOT
    / "engine"
    / "Atomic-Stockfish"
    / "src"
    / ("atomic-stockfish-data-tools.exe" if os.name == "nt" else "atomic-stockfish-data-tools")
)
HEX_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class LauncherError(RuntimeError):
    """The authenticated launcher contract could not be satisfied."""


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise LauncherError(message)


def _load_expected_capabilities(lock_path: Path) -> bytes:
    try:
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise LauncherError(f"cannot read Atomic engine lock: {error}") from error

    _require(isinstance(lock, dict), "Atomic engine lock root must be an object")
    _require(
        type(lock.get("schema_version")) is int and lock["schema_version"] == 2,
        "unsupported Atomic engine lock schema_version",
    )
    contract = lock.get("data_tools_contract")
    _require(isinstance(contract, dict), "data_tools_contract must be an object")
    _require(
        set(contract)
        == {
            "contract_version",
            "data_schema",
            "manifest_schema",
            "capabilities",
        },
        "data_tools_contract has missing or unknown fields",
    )
    _require(
        type(contract["contract_version"]) is int
        and contract["contract_version"] == 1,
        "unsupported data-tools contract version",
    )

    schemas: dict[str, dict[str, str]] = {}
    for field, expected_path in (
        ("data_schema", "schemas/atomic-bin-v2.json"),
        ("manifest_schema", "schemas/atomic-bin-v2-manifest.json"),
    ):
        schema = contract[field]
        _require(isinstance(schema, dict), f"{field} must be an object")
        _require(set(schema) == {"path", "sha256"}, f"{field} has invalid fields")
        _require(
            isinstance(schema["path"], str) and schema["path"] == expected_path,
            f"{field}.path is not canonical",
        )
        _require(
            isinstance(schema["sha256"], str)
            and HEX_SHA256_RE.fullmatch(schema["sha256"]) is not None,
            f"{field}.sha256 must be a lowercase SHA-256",
        )
        schemas[field] = schema

    capability_text = contract["capabilities"]
    _require(isinstance(capability_text, str), "capabilities must be a string")
    _require(
        capability_text.endswith("\n")
        and capability_text.count("\n") == 1
        and "\r" not in capability_text,
        "capabilities must be exactly one LF-terminated line",
    )
    try:
        capability = json.loads(capability_text)
    except json.JSONDecodeError as error:
        raise LauncherError(f"capabilities is not valid JSON: {error}") from error
    _require(
        json.dumps(capability, ensure_ascii=False, separators=(",", ":")) + "\n"
        == capability_text,
        "capabilities must be canonical minified JSON",
    )
    _require(
        isinstance(capability, dict)
        and set(capability) == {"type", "contract_version", "formats"},
        "capabilities object has invalid fields",
    )
    _require(
        capability["type"] == "atomic-data-tools-capabilities"
        and type(capability["contract_version"]) is int
        and capability["contract_version"] == contract["contract_version"],
        "capabilities identity does not match the lock contract",
    )
    formats = capability["formats"]
    _require(
        isinstance(formats, dict) and set(formats) == {"atomic-bin-v2"},
        "capabilities must expose only atomic-bin-v2",
    )
    v2 = formats["atomic-bin-v2"]
    _require(
        isinstance(v2, dict)
        and set(v2)
        == {
            "data_schema_sha256",
            "manifest_schema_sha256",
            "entrypoint",
            "read",
            "write",
            "operations",
        },
        "atomic-bin-v2 capability has invalid fields",
    )
    _require(
        v2["data_schema_sha256"] == schemas["data_schema"]["sha256"]
        and v2["manifest_schema_sha256"] == schemas["manifest_schema"]["sha256"]
        and v2["entrypoint"] == "manifest"
        and v2["read"] is True
        and v2["write"] is False
        and v2["operations"] == ["validate"],
        "atomic-bin-v2 capability does not match the locked schemas and operations",
    )
    return capability_text.encode("utf-8")


def _run_child(command: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            shell=False,
        )
    except (OSError, ValueError) as error:
        raise LauncherError(f"cannot execute pinned Atomic data-tools: {error}") from error


def _verify_source_pin(command: Sequence[str]) -> None:
    if not command:
        raise LauncherError("internal source-pin verifier command is empty")
    try:
        completed = subprocess.run(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            shell=False,
        )
    except (OSError, ValueError) as error:
        raise LauncherError(f"cannot execute Atomic source-pin verifier: {error}") from error
    if completed.returncode != 0:
        detail = (completed.stdout + completed.stderr).decode(
            "utf-8", errors="replace"
        ).strip()
        suffix = f": {detail}" if detail else ""
        raise LauncherError(f"Atomic source-pin verification failed{suffix}")


def _write(stream: BinaryIO, payload: bytes) -> None:
    stream.write(payload)
    stream.flush()


def _error(stream: BinaryIO, message: str) -> int:
    _write(stream, f"Atomic BIN V2 launcher error: {message}\n".encode("utf-8"))
    return 3


def main(
    argv: Sequence[str] | None = None,
    *,
    _child_command: Sequence[str] | None = None,
    _pin_command: Sequence[str] | None = None,
    _lock_path: Path | None = None,
    _stdout: BinaryIO | None = None,
    _stderr: BinaryIO | None = None,
) -> int:
    """Authenticate the pinned child and relay its command byte-for-byte.

    Underscored keyword arguments are dependency-injection seams for unit tests;
    they are intentionally unavailable through the public command line.
    """

    arguments = list(sys.argv[1:] if argv is None else argv)
    child_command = list(_child_command or (str(ENGINE_TOOLS),))
    pin_command = list(
        _pin_command or (sys.executable, str(PIN_VERIFIER))
    )
    stdout = _stdout or sys.stdout.buffer
    stderr = _stderr or sys.stderr.buffer

    if not child_command:
        return _error(stderr, "internal child command is empty")
    try:
        expected = _load_expected_capabilities((_lock_path or LOCK_PATH).resolve())
        _verify_source_pin(pin_command)
        probe = _run_child([*child_command, "capabilities"])
    except LauncherError as error:
        return _error(stderr, str(error))

    if probe.returncode != 0 or probe.stderr != b"" or probe.stdout != expected:
        return _error(stderr, "pinned child capabilities do not match atomic-engine.lock.json")

    if arguments == ["capabilities"]:
        _write(stdout, probe.stdout)
        return 0

    try:
        completed = _run_child([*child_command, *arguments])
    except LauncherError as error:
        return _error(stderr, str(error))
    if completed.returncode not in {0, 2, 3}:
        return _error(stderr, f"pinned child returned unsupported exit code {completed.returncode}")
    _write(stdout, completed.stdout)
    _write(stderr, completed.stderr)
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
