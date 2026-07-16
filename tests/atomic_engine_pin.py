#!/usr/bin/env python3
"""Fail-closed verification for the pinned Atomic-Stockfish submodule."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
from typing import Any, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOCK = REPO_ROOT / "atomic-engine.lock.json"
HEX_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
HEX_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
V2_DATA_SCHEMA_PATH = "schemas/atomic-bin-v2.json"
V2_DATA_SCHEMA_SHA256 = (
    "0352b036f2a140c609e3eb9c9d635dc553e8d77253d8faa92437390f5cf93cb6"
)
V2_MANIFEST_SCHEMA_PATH = "schemas/atomic-bin-v2-manifest.json"
V2_MANIFEST_SCHEMA_SHA256 = (
    "83d63922df3ac4a0c81a21ec9d9fd9e180efe50f26efee62fe01710e09da5b42"
)
V2_DECODE_SCHEMA_PATH = "schemas/atomic-data-tools-decode-v1.json"
V2_DECODE_SCHEMA_SHA256 = (
    "5e3f8d7c6db6ee955b71747ee063859e15609adb557a3754228a606f3df2caad"
)
DATA_TOOLS_CAPABILITIES = (
    '{"type":"atomic-data-tools-capabilities","contract_version":1,'
    '"formats":{"atomic-bin-v2":{"data_schema_sha256":'
    f'"{V2_DATA_SCHEMA_SHA256}","manifest_schema_sha256":'
    f'"{V2_MANIFEST_SCHEMA_SHA256}","decode_schema_sha256":'
    f'"{V2_DECODE_SCHEMA_SHA256}","entrypoint":"manifest","read":true,'
    '"write":false,"operations":["validate","decode"]}}}\n'
)


def find_git() -> str:
    configured = os.environ.get("GIT")
    discovered = shutil.which("git")
    candidates = (
        configured,
        discovered,
        r"C:\Program Files\Git\cmd\git.exe",
        r"C:\Program Files\Git\bin\git.exe",
        r"C:\msys64\usr\bin\git.exe",
    )
    for candidate in candidates:
        if candidate and (Path(candidate).is_file() or candidate == discovered):
            return candidate
    return "git"


GIT = find_git()


class PinError(RuntimeError):
    """The checked-out engine does not match the authenticated lock."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise PinError(message)


def run_git(root: Path, *arguments: str, check: bool = True) -> str:
    completed = subprocess.run(
        [GIT, "-C", str(root), *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if check and completed.returncode != 0:
        raise PinError(
            "git {} failed in {}: {}".format(
                " ".join(arguments), root, completed.stderr.strip()
            )
        )
    return completed.stdout.strip()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_relative_path(raw: object, label: str) -> PurePosixPath:
    require(isinstance(raw, str) and raw != "", f"{label} must be a non-empty string")
    require(
        "\\" not in raw and ":" not in raw and "\x00" not in raw,
        f"{label} must use a canonical repository-relative POSIX path",
    )
    path = PurePosixPath(raw)
    require(
        path.parts
        and raw == path.as_posix()
        and not path.is_absolute()
        and all(part not in {".", ".."} for part in path.parts),
        f"{label} must use a canonical repository-relative POSIX path",
    )
    return path


def resolved_repository_path(root: Path, raw: object, label: str) -> Path:
    relative = safe_relative_path(raw, label)
    resolved_root = root.resolve()
    resolved = resolved_root.joinpath(*relative.parts).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as error:
        raise PinError(f"{label} resolves outside its repository root") from error
    return resolved


def load_lock(path: Path, contents: str | None = None) -> dict[str, Any]:
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8") if contents is None else contents
        )
    except (OSError, json.JSONDecodeError) as error:
        raise PinError(f"cannot read engine lock {path}: {error}") from error
    require(isinstance(payload, dict), "engine lock root must be an object")
    require(
        set(payload)
        == {
            "schema_version",
            "submodule",
            "data_schema",
            "build_contract",
            "data_tools_contract",
        },
        "engine lock has missing or unknown top-level fields",
    )
    require(payload["schema_version"] == 2, "unsupported engine lock schema_version")

    submodule = payload["submodule"]
    require(isinstance(submodule, dict), "submodule lock must be an object")
    require(
        set(submodule) == {"name", "path", "url", "commit", "required_ref"},
        "submodule lock has missing or unknown fields",
    )
    safe_relative_path(submodule["path"], "submodule.path")
    require(submodule["name"] == submodule["path"], "submodule name and path must match")
    require(
        submodule["url"] == "https://github.com/Belzedar94/Atomic-Stockfish.git",
        "submodule URL is not the canonical Atomic-Stockfish repository",
    )
    require(
        isinstance(submodule["commit"], str)
        and HEX_SHA1_RE.fullmatch(submodule["commit"]) is not None,
        "submodule commit must be a lowercase 40-digit Git SHA",
    )
    require(
        submodule["required_ref"] == "refs/remotes/origin/main",
        "submodule required_ref must be refs/remotes/origin/main",
    )

    schema = payload["data_schema"]
    require(isinstance(schema, dict), "data_schema lock must be an object")
    require(
        set(schema)
        == {"path", "sha256", "schema_id", "variant", "record_size", "atomic960"},
        "data_schema lock has missing or unknown fields",
    )
    safe_relative_path(schema["path"], "data_schema.path")
    require(
        isinstance(schema["sha256"], str)
        and HEX_SHA256_RE.fullmatch(schema["sha256"]) is not None,
        "data schema hash must be a lowercase 64-digit SHA-256",
    )
    require(schema["schema_id"] == "legacy-atomic-v1", "unexpected data schema id")
    require(schema["variant"] == "atomic", "data schema variant must be atomic")
    require(schema["record_size"] == 72, "Legacy Atomic V1 record size must be 72")
    require(schema["atomic960"] is False, "Legacy Atomic V1 must reject Atomic960")

    build = payload["build_contract"]
    require(isinstance(build, dict), "build_contract lock must be an object")
    require(
        set(build)
        == {
            "playing_target",
            "data_generator_target",
            "data_tools_target",
            "playing_artifacts",
            "data_generator_artifacts",
            "data_tools_artifacts",
        },
        "build_contract lock has missing or unknown fields",
    )
    require(build["playing_target"] == "build", "unexpected playing-engine target")
    require(
        build["data_generator_target"] == "data-generator",
        "unexpected data-generator target",
    )
    require(build["data_tools_target"] == "data-tools", "unexpected data-tools target")
    expected_artifacts = {
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
    }
    for field, expected in expected_artifacts.items():
        require(build[field] == expected, f"unexpected {field} contract")

    tools = payload["data_tools_contract"]
    require(isinstance(tools, dict), "data_tools_contract lock must be an object")
    require(
        set(tools)
        == {
            "contract_version",
            "data_schema",
            "manifest_schema",
            "decode_schema",
            "capabilities",
        },
        "data_tools_contract lock has missing or unknown fields",
    )
    require(
        type(tools["contract_version"]) is int and tools["contract_version"] == 1,
        "unsupported data-tools contract_version",
    )

    expected_schemas = {
        "data_schema": (V2_DATA_SCHEMA_PATH, V2_DATA_SCHEMA_SHA256),
        "manifest_schema": (V2_MANIFEST_SCHEMA_PATH, V2_MANIFEST_SCHEMA_SHA256),
        "decode_schema": (V2_DECODE_SCHEMA_PATH, V2_DECODE_SCHEMA_SHA256),
    }
    for field, (expected_path, expected_sha256) in expected_schemas.items():
        locked_schema = tools[field]
        require(isinstance(locked_schema, dict), f"{field} lock must be an object")
        require(
            set(locked_schema) == {"path", "sha256"},
            f"{field} lock has missing or unknown fields",
        )
        safe_relative_path(locked_schema["path"], f"data_tools_contract.{field}.path")
        require(
            locked_schema["path"] == expected_path,
            f"unexpected data-tools {field} path",
        )
        require(
            isinstance(locked_schema["sha256"], str)
            and HEX_SHA256_RE.fullmatch(locked_schema["sha256"]) is not None,
            f"data-tools {field} hash must be a lowercase 64-digit SHA-256",
        )
        require(
            locked_schema["sha256"] == expected_sha256,
            f"unexpected data-tools {field} SHA-256",
        )

    capabilities = tools["capabilities"]
    require(isinstance(capabilities, str), "data-tools capabilities must be a string")
    require(
        capabilities.endswith("\n")
        and "\r" not in capabilities
        and "\n" not in capabilities[:-1],
        "data-tools capabilities must be exactly one LF-terminated JSON line",
    )
    try:
        decoded_capabilities = json.loads(capabilities)
    except json.JSONDecodeError as error:
        raise PinError(f"data-tools capabilities is not valid JSON: {error}") from error
    canonical_capabilities = (
        json.dumps(decoded_capabilities, ensure_ascii=False, separators=(",", ":")) + "\n"
    )
    require(
        capabilities == canonical_capabilities,
        "data-tools capabilities must use canonical minified JSON",
    )
    require(
        capabilities == DATA_TOOLS_CAPABILITIES,
        "unexpected data-tools capabilities contract",
    )
    return payload


def index_file(root: Path, relative_path: str, expected_mode: str) -> str:
    stage = run_git(root, "ls-files", "--stage", "--", relative_path)
    entries = [line for line in stage.splitlines() if line]
    require(len(entries) == 1, f"{relative_path} must have exactly one index entry")
    metadata, separator, indexed_path = entries[0].partition("\t")
    fields = metadata.split()
    require(
        separator == "\t"
        and indexed_path == relative_path
        and len(fields) == 3
        and fields[0] == expected_mode
        and fields[2] == "0",
        f"{relative_path} has an invalid or conflicted index entry",
    )
    indexed = run_git(root, "show", f":{relative_path}")
    worktree_path = resolved_repository_path(root, relative_path, relative_path)
    try:
        worktree = worktree_path.read_text(encoding="utf-8").rstrip("\r\n")
    except OSError as error:
        raise PinError(f"cannot read {worktree_path}: {error}") from error
    require(
        worktree == indexed.rstrip("\r\n"),
        f"{relative_path} worktree does not match the authenticated index snapshot",
    )
    return indexed


def git_exit_code(root: Path, *arguments: str) -> int:
    completed = subprocess.run(
        [GIT, "-C", str(root), *arguments],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    return completed.returncode


def gitmodule_value(root: Path, name: str, field: str) -> str:
    completed = subprocess.run(
        [
            GIT,
            "config",
            "-f",
            str(root / ".gitmodules"),
            "--get",
            f"submodule.{name}.{field}",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        raise PinError(f".gitmodules has no submodule.{name}.{field}")
    return completed.stdout.strip()


def gitmodule_fields(root: Path, name: str) -> set[str]:
    completed = subprocess.run(
        [
            GIT,
            "config",
            "-f",
            str(root / ".gitmodules"),
            "--name-only",
            "--get-regexp",
            rf"^submodule\.{re.escape(name)}\.",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        raise PinError(f".gitmodules has no definition for submodule {name}")
    return set(completed.stdout.splitlines())


def verify_engine_pin(root: Path = REPO_ROOT, lock_path: Path | None = None) -> dict[str, Any]:
    root = root.resolve()
    lock_path = (lock_path or root / DEFAULT_LOCK.name).resolve()
    try:
        lock_relative = lock_path.relative_to(root).as_posix()
    except ValueError as error:
        raise PinError("engine lock must be inside the tools repository") from error
    require(lock_relative == DEFAULT_LOCK.name, "engine lock must use the canonical path")
    lock = load_lock(lock_path, index_file(root, lock_relative, "100644"))
    submodule_lock = lock["submodule"]
    submodule_path = resolved_repository_path(
        root, submodule_lock["path"], "submodule.path"
    )

    require((root / ".gitmodules").is_file(), ".gitmodules is missing")
    index_file(root, ".gitmodules", "100644")
    require(submodule_path.is_dir(), f"Atomic-Stockfish submodule is missing: {submodule_path}")
    require(
        gitmodule_value(root, submodule_lock["name"], "path") == submodule_lock["path"],
        ".gitmodules path does not match the lock",
    )
    require(
        gitmodule_value(root, submodule_lock["name"], "url") == submodule_lock["url"],
        ".gitmodules URL does not match the lock",
    )
    require(
        gitmodule_fields(root, submodule_lock["name"])
        == {
            f"submodule.{submodule_lock['name']}.path",
            f"submodule.{submodule_lock['name']}.url",
        },
        ".gitmodules has missing or unknown submodule fields",
    )

    stage = run_git(root, "ls-files", "--stage", "--", submodule_lock["path"])
    entries = [line for line in stage.splitlines() if line]
    require(len(entries) == 1, "engine path must have exactly one index entry")
    metadata, separator, indexed_path = entries[0].partition("\t")
    fields = metadata.split()
    require(
        separator == "\t"
        and indexed_path == submodule_lock["path"]
        and len(fields) == 3
        and fields[0] == "160000"
        and fields[2] == "0",
        "engine path is not a stage-0 Git submodule",
    )
    require(fields[1] == submodule_lock["commit"], "superproject gitlink does not match the lock")
    require(
        run_git(submodule_path, "rev-parse", "HEAD") == submodule_lock["commit"],
        "checked-out engine commit does not match the lock",
    )
    require(run_git(submodule_path, "status", "--porcelain", "--untracked-files=all") == "", "engine submodule is dirty")
    require(
        run_git(submodule_path, "remote", "get-url", "origin") == submodule_lock["url"],
        "engine origin URL does not match the lock",
    )
    required_ref = submodule_lock["required_ref"]
    require(
        git_exit_code(submodule_path, "show-ref", "--verify", "--quiet", required_ref)
        == 0,
        f"engine required ref is missing: {required_ref}",
    )
    require(
        git_exit_code(
            submodule_path,
            "merge-base",
            "--is-ancestor",
            submodule_lock["commit"],
            required_ref,
        )
        == 0,
        "locked engine commit is not merged into origin/main",
    )

    schema_lock = lock["data_schema"]
    schema_path = resolved_repository_path(
        submodule_path, schema_lock["path"], "data_schema.path"
    )
    require(schema_path.is_file(), f"pinned data schema is missing: {schema_path}")
    actual_schema_sha = sha256(schema_path)
    require(actual_schema_sha == schema_lock["sha256"], "pinned data schema SHA-256 mismatch")
    try:
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PinError(f"cannot parse pinned data schema: {error}") from error
    require(schema.get("schema_id") == schema_lock["schema_id"], "pinned schema id mismatch")
    require(schema.get("variant") == schema_lock["variant"], "pinned schema variant mismatch")
    require(schema.get("format", {}).get("record_size") == schema_lock["record_size"], "pinned schema record size mismatch")
    require(schema.get("format", {}).get("atomic960") is schema_lock["atomic960"], "pinned schema Atomic960 capability mismatch")

    tools_lock = lock["data_tools_contract"]
    authenticated_schemas: dict[str, dict[str, Any]] = {}
    for field in ("data_schema", "manifest_schema", "decode_schema"):
        schema_contract = tools_lock[field]
        authenticated_path = resolved_repository_path(
            submodule_path,
            schema_contract["path"],
            f"data_tools_contract.{field}.path",
        )
        require(
            authenticated_path.is_file(),
            f"pinned data-tools {field} is missing: {authenticated_path}",
        )
        require(
            sha256(authenticated_path) == schema_contract["sha256"],
            f"pinned data-tools {field} SHA-256 mismatch",
        )
        try:
            decoded_schema = json.loads(authenticated_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise PinError(f"cannot parse pinned data-tools {field}: {error}") from error
        require(
            isinstance(decoded_schema, dict),
            f"pinned data-tools {field} root must be an object",
        )
        authenticated_schemas[field] = decoded_schema

    v2_schema = authenticated_schemas["data_schema"]
    require(v2_schema.get("schema_version") == 2, "pinned V2 data schema version mismatch")
    require(v2_schema.get("schema_id") == "atomic-bin-v2", "pinned V2 data schema id mismatch")
    require(v2_schema.get("variant") == "atomic", "pinned V2 data schema variant mismatch")

    manifest_schema = authenticated_schemas["manifest_schema"]
    require(
        manifest_schema.get("schema_version") == 1,
        "pinned V2 manifest schema version mismatch",
    )
    require(
        manifest_schema.get("$id")
        == "urn:atomic-stockfish:schema:atomic-bin-v2-manifest:1",
        "pinned V2 manifest schema id mismatch",
    )
    require(
        manifest_schema.get("properties", {})
        .get("data_schema_sha256", {})
        .get("const")
        == V2_DATA_SCHEMA_SHA256,
        "pinned V2 manifest data-schema binding mismatch",
    )

    decode_schema = authenticated_schemas["decode_schema"]
    require(
        decode_schema.get("schema_version") == 1,
        "pinned V2 decode schema version mismatch",
    )
    require(
        decode_schema.get("$id")
        == "urn:atomic-stockfish:schema:atomic-data-tools-decode:1",
        "pinned V2 decode schema id mismatch",
    )
    require(
        decode_schema.get("oneOf")
        == [
            {"$ref": "#/$defs/header"},
            {"$ref": "#/$defs/record"},
            {"$ref": "#/$defs/footer"},
        ],
        "pinned V2 decode schema stream variants mismatch",
    )
    decode_contract = decode_schema.get("x-jsonl-contract")
    require(
        isinstance(decode_contract, dict)
        and decode_contract.get("encoding") == "UTF-8"
        and decode_contract.get("bom") is False
        and decode_contract.get("line_ending") == "LF"
        and decode_contract.get("key_order") == "schema-declaration-order"
        and decode_contract.get("sequence")
        == [
            "atomic-data-tools-decode-header",
            "atomic-data-tools-decode-record repeated slice.limit times",
            "atomic-data-tools-decode-footer",
        ],
        "pinned V2 decode JSONL contract mismatch",
    )
    decode_definitions = decode_schema.get("$defs")
    decode_header = (
        decode_definitions.get("header")
        if isinstance(decode_definitions, dict)
        else None
    )
    decode_properties = (
        decode_header.get("properties") if isinstance(decode_header, dict) else None
    )
    decode_hash_property = (
        decode_properties.get("decode_schema_sha256")
        if isinstance(decode_properties, dict)
        else None
    )
    require(
        isinstance(decode_hash_property, dict)
        and decode_hash_property.get("x-value")
        == "sha256-of-exact-decode-schema-file",
        "pinned V2 decode schema self-binding mismatch",
    )

    engine_makefile = submodule_path / "src" / "Makefile"
    require(engine_makefile.is_file(), "pinned engine Makefile is missing")
    makefile = engine_makefile.read_text(encoding="utf-8")
    for target in (
        lock["build_contract"]["playing_target"],
        lock["build_contract"]["data_generator_target"],
        lock["build_contract"]["data_tools_target"],
    ):
        require(
            re.search(rf"^{re.escape(target)}\s*:", makefile, re.MULTILINE) is not None,
            f"pinned engine does not expose target {target}",
        )

    return lock


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=REPO_ROOT)
    parser.add_argument("--lock", type=Path)
    parser.add_argument(
        "--print-commit",
        action="store_true",
        help="emit only the authenticated engine commit",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        lock = verify_engine_pin(args.root, args.lock)
    except PinError as error:
        print(
            f"Atomic engine pin verification failed: {error}",
            file=sys.stderr if args.print_commit else sys.stdout,
        )
        return 1
    if args.print_commit:
        print(lock["submodule"]["commit"])
        return 0
    print(
        "Atomic engine pin verified "
        f"commit={lock['submodule']['commit']} "
        f"legacy_schema_sha256={lock['data_schema']['sha256']} "
        f"data_tools_contract={lock['data_tools_contract']['contract_version']} "
        f"v2_schema_sha256={lock['data_tools_contract']['data_schema']['sha256']} "
        f"decode_schema_sha256={lock['data_tools_contract']['decode_schema']['sha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
