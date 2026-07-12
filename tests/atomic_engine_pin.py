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
from typing import Any, Sequence


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_LOCK = REPO_ROOT / "atomic-engine.lock.json"
HEX_SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
HEX_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


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
    path = PurePosixPath(raw)
    require(not path.is_absolute() and ".." not in path.parts, f"{label} must be repository-relative")
    return path


def load_lock(path: Path, contents: str | None = None) -> dict[str, Any]:
    try:
        payload = json.loads(
            path.read_text(encoding="utf-8") if contents is None else contents
        )
    except (OSError, json.JSONDecodeError) as error:
        raise PinError(f"cannot read engine lock {path}: {error}") from error
    require(isinstance(payload, dict), "engine lock root must be an object")
    require(
        set(payload) == {"schema_version", "submodule", "data_schema", "build_contract"},
        "engine lock has missing or unknown top-level fields",
    )
    require(payload["schema_version"] == 1, "unsupported engine lock schema_version")

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
            "playing_artifacts",
            "data_generator_artifacts",
        },
        "build_contract lock has missing or unknown fields",
    )
    require(build["playing_target"] == "build", "unexpected playing-engine target")
    require(
        build["data_generator_target"] == "data-generator",
        "unexpected data-generator target",
    )
    expected_artifacts = {
        "playing_artifacts": {
            "linux": "src/atomic-stockfish",
            "windows": "src/atomic-stockfish.exe",
        },
        "data_generator_artifacts": {
            "linux": "src/atomic-stockfish-data-generator",
            "windows": "src/atomic-stockfish-data-generator.exe",
        },
    }
    for field, expected in expected_artifacts.items():
        require(build[field] == expected, f"unexpected {field} contract")
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
    worktree_path = root.joinpath(*PurePosixPath(relative_path).parts)
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
    submodule_path = root.joinpath(*PurePosixPath(submodule_lock["path"]).parts)

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
    schema_path = submodule_path.joinpath(*PurePosixPath(schema_lock["path"]).parts)
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

    engine_makefile = submodule_path / "src" / "Makefile"
    require(engine_makefile.is_file(), "pinned engine Makefile is missing")
    makefile = engine_makefile.read_text(encoding="utf-8")
    for target in (
        lock["build_contract"]["playing_target"],
        lock["build_contract"]["data_generator_target"],
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
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        lock = verify_engine_pin(args.root, args.lock)
    except PinError as error:
        print(f"Atomic engine pin verification failed: {error}")
        return 1
    print(
        "Atomic engine pin verified "
        f"commit={lock['submodule']['commit']} "
        f"schema_sha256={lock['data_schema']['sha256']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
