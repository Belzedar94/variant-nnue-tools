#!/usr/bin/env python3
"""Independent symbolic reachability oracle for AtomicNNUEV3.

The mask generator is deliberately stdlib-only and dataset-independent.  It
authenticates one frozen AtomicNNUEV3 feature schema, derives the eight physical
reachability bitmaps from that specification, and publishes the 18,772-byte
wire plus a canonical manifest as one fail-closed transaction.

The separate ``attest`` command only composes the H9.3l-a reachability
attestation after a controlling release job supplies explicit, authenticated
campaign/producer/policy descriptors.  This module does not authenticate a
distributed producer by itself and never imports engine, trainer, or dataset
implementation code.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import struct
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, MutableMapping, Sequence, Tuple


ALGORITHM_VERSION = "atomic-v3-symbolic-reachability-v1"
MANIFEST_SCHEMA_ID = "atomic-v3-symbolic-reachability-manifest-v1"
CONTROLLER_SCHEMA_ID = "atomic-v3-reachability-controller-v1"
UPSTREAM_REPOSITORY = "https://github.com/Belzedar94/Atomic-Stockfish"
UPSTREAM_CONTRACT_COMMIT = "dde43fc08fb2bd45eec09d3dbe9f6d06845eeb24"
FEATURE_SCHEMA_SHA256 = "9d3c77a58e5e55ac1bc798dab41977451eb523fce1d6fd3ec3f7c1e574a78750"
REACHABILITY_SCHEMA_SHA256 = (
    "fb1af7130a2fa74be0fadd721db980269e12c89204b627eec63e6074ed3983e8"
)
CAMPAIGN_SCHEMA_SHA256 = "36a86983d63e71e20daa3bcf7a574dfc95abb544974e36c064445e79ad706517"
PRODUCER_SCHEMA_SHA256 = "de55f384fdea56fdb28addd50b78da7e0256b5a8857d5aec856219a3e922193e"
COVERAGE_POLICY_SCHEMA_SHA256 = (
    "c496a694df56efd4d221e86c9772f79b02a48ef8955b41724804555abeba3b9d"
)

PERSPECTIVES = ("WHITE", "BLACK")
SLICE_IDS = (
    "half-ka-v2-atomic-hm",
    "atomic-capture-pair",
    "atomic-king-blast-ep",
    "atomic-blast-ring",
)
MASK_FIELDS = tuple(identifier.replace("-", "_") for identifier in SLICE_IDS)
DERIVED_FIELDS = ("hm_training", "hm_virtual_factors")
MASK_KIND_IDS = {"physical": 0, "training": 1, "virtual-factor": 2}
MASK_DOMAIN = b"atomic-v3-reachability-mask-v2\0"
MASK_AGGREGATE_DOMAIN = b"atomic-v3-reachability-set-v2\0"
ATTESTATION_DOMAIN = b"atomic-v3-reachability-attestation-v1\0"

EXPECTED_LAYOUT = (
    ("half-ka-v2-atomic-hm", 0, 22_528, 2_816),
    ("atomic-capture-pair", 22_528, 40_012, 5_002),
    ("atomic-king-blast-ep", 62_540, 2_304, 288),
    ("atomic-blast-ring", 64_844, 10_240, 1_280),
)
OUTPUT_BYTES = 18_772
MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_FEATURE_SCHEMA_BYTES = 1 * 1024 * 1024
MAX_U64 = (1 << 64) - 1
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
COMMIT_RE = re.compile(r"[0-9a-f]{40}\Z")
U64_RE = re.compile(r"(?:0|[1-9][0-9]*)\Z")
BASENAME_RE = re.compile(r'^(?!\.{1,2}$)[^/\\:\x00<>"|?*]+\Z')

DIRECTIONS = (
    ("N", 0, 1),
    ("NE", 1, 1),
    ("E", 1, 0),
    ("SE", 1, -1),
    ("S", 0, -1),
    ("SW", -1, -1),
    ("W", -1, 0),
    ("NW", -1, 1),
)
DIRECTION_BY_NAME = {name: (df, dr) for name, df, dr in DIRECTIONS}
KNIGHT_DELTAS = (
    (-2, -1),
    (-2, 1),
    (-1, -2),
    (-1, 2),
    (1, -2),
    (1, 2),
    (2, -1),
    (2, 1),
)

CONTROLLER_VERIFICATION_FIELDS = (
    "campaign_authenticated",
    "producer_attestation_authenticated",
    "coverage_policy_authenticated",
    "oracle_binary_authenticated",
)
ATTESTATION_VERIFICATION_FIELDS = (
    "campaign_authenticated",
    "producer_attestation_authenticated",
    "feature_schema_authenticated",
    "oracle_binary_authenticated",
    "oracle_used_no_dataset_artifacts",
    "all_physical_masks_reproduced",
    "hm_training_projection_recomputed",
    "hm_virtual_projection_recomputed",
    "aggregate_hash_recomputed",
    "all_campaign_policy_masks_matched",
    "strict_eof",
)


class OracleError(RuntimeError):
    """A fail-closed contract, input, or publication error."""


class DuplicateKeyError(ValueError):
    """Strict JSON input repeated a key."""


@dataclass(frozen=True)
class FileSnapshot:
    payload: bytes
    byte_count: int
    sha256: str


@dataclass(frozen=True)
class OracleResult:
    feature_schema_file: str
    feature_schema_bytes: int
    feature_schema_sha256: str
    output: bytes
    roles: Mapping[str, Mapping[str, Mapping[str, Any]]]
    aggregate_sha256: str
    physical_layout: Sequence[Mapping[str, Any]]


def _reject_duplicate_keys(pairs: Sequence[Tuple[str, Any]]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateKeyError("duplicate JSON key: " + key)
        result[key] = value
    return result


def _reject_non_json_number(value: str) -> None:
    raise ValueError("non-JSON numeric constant: " + value)


def _decode_json(payload: bytes, label: str) -> Dict[str, Any]:
    if payload.startswith(b"\xef\xbb\xbf"):
        raise OracleError(label + ": UTF-8 BOM is forbidden")
    try:
        value = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_json_number,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, DuplicateKeyError, ValueError) as exc:
        raise OracleError(label + ": invalid strict JSON: " + str(exc)) from exc
    if not isinstance(value, dict):
        raise OracleError(label + ": top-level JSON value must be an object")
    return value


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")


def _is_reparse_point(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & marker)


def _assert_no_link(path: Path, label: str, include_leaf: bool = True) -> None:
    absolute = Path(os.path.abspath(os.fspath(path)))
    candidates = [absolute] if include_leaf else []
    candidates.extend(absolute.parents)
    for candidate in candidates:
        try:
            metadata = os.lstat(candidate)
        except FileNotFoundError:
            if candidate == absolute and not include_leaf:
                continue
            if candidate == absolute:
                raise OracleError(label + ": path does not exist")
            continue
        if stat.S_ISLNK(metadata.st_mode) or _is_reparse_point(metadata):
            raise OracleError(label + ": symbolic links and reparse points are forbidden")


def _identity(metadata: os.stat_result) -> Tuple[int, int, int, int]:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_size),
        int(getattr(metadata, "st_mtime_ns", int(metadata.st_mtime * 1_000_000_000))),
    )


def _handle_identity(metadata: os.stat_result) -> Tuple[int, int, int, int, int]:
    return _identity(metadata) + (
        int(getattr(metadata, "st_ctime_ns", int(metadata.st_ctime * 1_000_000_000))),
    )


def _read_regular_snapshot(path: Path, maximum: int, label: str) -> FileSnapshot:
    _assert_no_link(path, label)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise OracleError(label + ": cannot open regular file: " + str(exc)) from exc
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise OracleError(label + ": input must be a regular file")
        if before.st_size <= 0 or before.st_size > maximum:
            raise OracleError(
                "{}: byte size {} is outside 1..{}".format(label, before.st_size, maximum)
            )
        chunks = []
        remaining = maximum + 1
        while remaining:
            block = os.read(descriptor, min(1024 * 1024, remaining))
            if not block:
                break
            chunks.append(block)
            remaining -= len(block)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    if len(payload) > maximum:
        raise OracleError(label + ": input grew beyond the maximum size")
    if _handle_identity(before) != _handle_identity(after) or len(payload) != after.st_size:
        raise OracleError(label + ": file changed during same-handle snapshot")
    try:
        path_after = os.stat(path, follow_symlinks=False)
    except OSError as exc:
        raise OracleError(label + ": path changed after snapshot: " + str(exc)) from exc
    if _identity(after) != _identity(path_after):
        raise OracleError(label + ": path identity changed during snapshot")
    return FileSnapshot(payload, len(payload), hashlib.sha256(payload).hexdigest())


def _load_json_snapshot(
    path: Path, label: str, maximum: int = MAX_JSON_BYTES, canonical: bool = False
) -> Tuple[Dict[str, Any], FileSnapshot]:
    snapshot = _read_regular_snapshot(path, maximum, label)
    value = _decode_json(snapshot.payload, label)
    if canonical and snapshot.payload != _canonical_json(value):
        raise OracleError(label + ": JSON is not canonical declaration-order wire")
    return value, snapshot


def _require_keys(value: Mapping[str, Any], expected: Sequence[str], label: str) -> None:
    actual = tuple(value.keys())
    if actual != tuple(expected):
        raise OracleError(
            "{}: keys/order differ: expected {}, got {}".format(label, tuple(expected), actual)
        )


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise OracleError(label + ": must be an object")
    return value


def _sequence(value: Any, label: str) -> Sequence[Any]:
    if not isinstance(value, list):
        raise OracleError(label + ": must be an array")
    return value


def _safe_basename(value: Any, label: str) -> str:
    if not isinstance(value, str) or BASENAME_RE.fullmatch(value) is None:
        raise OracleError(label + ": must be a portable basename")
    return value


def _sha256(value: Any, label: str) -> str:
    if not isinstance(value, str) or SHA256_RE.fullmatch(value) is None:
        raise OracleError(label + ": must be lowercase SHA-256 hex")
    return value


def _positive_u64(value: Any, label: str) -> str:
    if not isinstance(value, str) or U64_RE.fullmatch(value) is None:
        raise OracleError(label + ": must be canonical decimal uint64 text")
    number = int(value)
    if number <= 0 or number > MAX_U64:
        raise OracleError(label + ": must be in 1..2^64-1")
    return value


def _artifact_descriptor(
    value: Any, label: str, typed: bool
) -> Mapping[str, Any]:
    descriptor = _mapping(value, label)
    keys = ("file", "bytes", "sha256", "schema_sha256") if typed else (
        "file",
        "bytes",
        "sha256",
    )
    _require_keys(descriptor, keys, label)
    _safe_basename(descriptor.get("file"), label + ".file")
    _positive_u64(descriptor.get("bytes"), label + ".bytes")
    _sha256(descriptor.get("sha256"), label + ".sha256")
    if typed:
        _sha256(descriptor.get("schema_sha256"), label + ".schema_sha256")
    return descriptor


def _bit_set(bitmap: bytearray, index: int) -> None:
    bitmap[index // 8] |= 1 << (index % 8)


def _bit(bitmap: bytes, index: int) -> int:
    return (bitmap[index // 8] >> (index % 8)) & 1


def _popcount(bitmap: bytes) -> int:
    return sum(byte.bit_count() for byte in bitmap)


def _square(file_index: int, rank_index: int) -> int:
    return rank_index * 8 + file_index


def _file(square: int) -> int:
    return square & 7


def _rank(square: int) -> int:
    return square >> 3


def _neighbor(square: int, direction: str) -> int | None:
    df, dr = DIRECTION_BY_NAME[direction]
    file_index = _file(square) + df
    rank_index = _rank(square) + dr
    if not 0 <= file_index < 8 or not 0 <= rank_index < 8:
        return None
    return _square(file_index, rank_index)


def _own_king_square(bucket: int) -> int:
    return (7 - bucket // 4) * 8 + (7 - bucket % 4)


def _feature_slices(schema: Mapping[str, Any]) -> Mapping[str, Mapping[str, Any]]:
    slices = _sequence(schema.get("feature_slices"), "feature_schema.feature_slices")
    result: Dict[str, Mapping[str, Any]] = {}
    for index, value in enumerate(slices):
        item = _mapping(value, "feature_schema.feature_slices[{}]".format(index))
        identifier = item.get("id")
        if not isinstance(identifier, str) or identifier in result:
            raise OracleError("feature_schema.feature_slices: invalid or duplicate id")
        result[identifier] = item
    if tuple(result) != SLICE_IDS:
        raise OracleError("feature_schema.feature_slices: frozen slice order differs")
    return result


def _authenticate_feature_schema(
    path: Path,
) -> Tuple[Mapping[str, Any], Mapping[str, Mapping[str, Any]], FileSnapshot]:
    schema, snapshot = _load_json_snapshot(
        path, "feature_schema", maximum=MAX_FEATURE_SCHEMA_BYTES
    )
    if snapshot.sha256 != FEATURE_SCHEMA_SHA256:
        raise OracleError(
            "feature_schema: SHA-256 mismatch; expected {}, got {}".format(
                FEATURE_SCHEMA_SHA256, snapshot.sha256
            )
        )
    if schema.get("backend") != "AtomicNNUEV3" or schema.get("schema_version") != 1:
        raise OracleError("feature_schema: not the frozen AtomicNNUEV3 v1 contract")
    orientation = _mapping(schema.get("orientation"), "feature_schema.orientation")
    if orientation.get("perspective_order") != list(PERSPECTIVES):
        raise OracleError("feature_schema.orientation: perspective order differs")
    if orientation.get("black_perspective_xor") != 56:
        raise OracleError("feature_schema.orientation: black XOR differs")
    if orientation.get("horizontal_mirror_xor") != 7:
        raise OracleError("feature_schema.orientation: horizontal XOR differs")

    slices = _feature_slices(schema)
    for identifier, offset, dimensions, _byte_count in EXPECTED_LAYOUT:
        item = slices[identifier]
        if item.get("physical_offset") != offset or item.get("physical_dimensions") != dimensions:
            raise OracleError("feature_schema.{}: physical layout differs".format(identifier))

    hm = slices[SLICE_IDS[0]]
    if hm.get("physical_plane_order") != [
        "OWN_PAWN",
        "OPP_PAWN",
        "OWN_KNIGHT",
        "OPP_KNIGHT",
        "OWN_BISHOP",
        "OPP_BISHOP",
        "OWN_ROOK",
        "OPP_ROOK",
        "OWN_QUEEN",
        "OPP_QUEEN",
        "MERGED_KING",
    ] or hm.get("king_buckets") != 32:
        raise OracleError("feature_schema.HM: frozen planes/buckets differ")

    capture = slices[SLICE_IDS[1]]
    if capture.get("actor_relation_order") != ["OWN", "OPP"]:
        raise OracleError("feature_schema.CapturePair: actor order differs")
    if capture.get("normal_target_class_order") != [
        "PAWN",
        "KNIGHT",
        "BISHOP",
        "ROOK",
        "QUEEN",
        "KING",
    ]:
        raise OracleError("feature_schema.CapturePair: target order differs")
    if capture.get("geometry_segment_bases") != {
        "PAWN": 0,
        "KNIGHT": 84,
        "BISHOP": 420,
        "ROOK": 980,
        "QUEEN": 1876,
    } or capture.get("geometry_dimensions") != 3332:
        raise OracleError("feature_schema.CapturePair: geometry contract differs")
    if capture.get("normal_dimensions") != 39_984 or capture.get("en_passant_dimensions") != 28:
        raise OracleError("feature_schema.CapturePair: compact tail differs")

    king_blast = slices[SLICE_IDS[2]]
    expected_king_classes = ["ENEMY_KING_CENTER"]
    expected_king_classes += ["ENEMY_KING_" + item[0] for item in DIRECTIONS]
    expected_king_classes += ["OWN_KING_" + item[0] for item in DIRECTIONS]
    expected_king_classes += ["EN_PASSANT_MARKER"]
    if king_blast.get("class_order") != expected_king_classes:
        raise OracleError("feature_schema.KingBlastEP: class order differs")

    ring = slices[SLICE_IDS[3]]
    if ring.get("offset_order") != [item[0] for item in DIRECTIONS]:
        raise OracleError("feature_schema.BlastRing: offset order differs")
    if ring.get("class_order") != [
        "KNIGHT",
        "BISHOP",
        "ROOK",
        "QUEEN",
        "ADJACENT_PAWN_SURVIVES",
    ]:
        raise OracleError("feature_schema.BlastRing: class order differs")
    return schema, slices, snapshot


def _hm_mask(hm: Mapping[str, Any]) -> bytes:
    dimensions = int(hm["physical_dimensions"])
    bitmap = bytearray((dimensions + 7) // 8)
    planes = tuple(hm["physical_plane_order"])
    buckets = int(hm["king_buckets"])
    if dimensions != buckets * len(planes) * 64:
        raise OracleError("feature_schema.HM: dimensions do not match axes")
    for bucket in range(buckets):
        own_king = _own_king_square(bucket)
        for plane, name in enumerate(planes):
            for square in range(64):
                reachable = True
                if name.endswith("PAWN") and _rank(square) in (0, 7):
                    reachable = False
                if name != "MERGED_KING" and square == own_king:
                    reachable = False
                if reachable:
                    index = (bucket * len(planes) + plane) * 64 + square
                    _bit_set(bitmap, index)
    return bytes(bitmap)


def _slider_edges(directions: Iterable[Tuple[int, int]]) -> Tuple[Tuple[int, int], ...]:
    edges = []
    for origin in range(64):
        targets = []
        for df, dr in directions:
            file_index = _file(origin) + df
            rank_index = _rank(origin) + dr
            while 0 <= file_index < 8 and 0 <= rank_index < 8:
                targets.append(_square(file_index, rank_index))
                file_index += df
                rank_index += dr
        edges.extend((origin, target) for target in sorted(targets))
    return tuple(edges)


def _capture_geometry(actor_relation: int) -> Mapping[str, Tuple[Tuple[int, int], ...]]:
    pawn_dr = 1 if actor_relation == 0 else -1
    pawn = []
    for origin in range(64):
        if _rank(origin) in (0, 7):
            continue
        targets = []
        for df in (-1, 1):
            file_index = _file(origin) + df
            rank_index = _rank(origin) + pawn_dr
            if 0 <= file_index < 8 and 0 <= rank_index < 8:
                targets.append(_square(file_index, rank_index))
        pawn.extend((origin, target) for target in sorted(targets))

    knight = []
    for origin in range(64):
        targets = []
        for df, dr in KNIGHT_DELTAS:
            file_index = _file(origin) + df
            rank_index = _rank(origin) + dr
            if 0 <= file_index < 8 and 0 <= rank_index < 8:
                targets.append(_square(file_index, rank_index))
        knight.extend((origin, target) for target in sorted(targets))

    diagonal = ((1, 1), (1, -1), (-1, 1), (-1, -1))
    orthogonal = ((1, 0), (-1, 0), (0, 1), (0, -1))
    bishop = _slider_edges(diagonal)
    rook = _slider_edges(orthogonal)
    queen = _slider_edges(diagonal + orthogonal)
    result = {
        "PAWN": tuple(pawn),
        "KNIGHT": tuple(knight),
        "BISHOP": bishop,
        "ROOK": rook,
        "QUEEN": queen,
    }
    expected = {"PAWN": 84, "KNIGHT": 336, "BISHOP": 560, "ROOK": 896, "QUEEN": 1456}
    actual = {name: len(edges) for name, edges in result.items()}
    if actual != expected:
        raise OracleError("symbolic CapturePair geometry differs: " + repr(actual))
    return result


def _capture_pair_mask(capture: Mapping[str, Any]) -> bytes:
    dimensions = int(capture["physical_dimensions"])
    bitmap = bytearray((dimensions + 7) // 8)
    targets = tuple(capture["normal_target_class_order"])
    bases = capture["geometry_segment_bases"]
    geometry_dimensions = int(capture["geometry_dimensions"])
    for actor_relation in range(2):
        geometry = _capture_geometry(actor_relation)
        for piece_name in ("PAWN", "KNIGHT", "BISHOP", "ROOK", "QUEEN"):
            base = int(bases[piece_name])
            for ordinal, (_origin, target_square) in enumerate(geometry[piece_name]):
                edge_ordinal = base + ordinal
                for target_class, target_name in enumerate(targets):
                    reachable = True
                    if target_name == "PAWN" and _rank(target_square) in (0, 7):
                        reachable = False
                    if target_name == "KING" and actor_relation == 1 and _file(target_square) < 4:
                        reachable = False
                    if reachable:
                        index = (actor_relation * geometry_dimensions + edge_ordinal) * 6 + target_class
                        _bit_set(bitmap, index)
    ep_offset = int(capture["en_passant_local_offset"])
    ep_count = int(capture["en_passant_dimensions"])
    for index in range(ep_offset, ep_offset + ep_count):
        _bit_set(bitmap, index)
    if ep_offset + ep_count != dimensions:
        raise OracleError("feature_schema.CapturePair: tail does not end at dimensions")
    return bytes(bitmap)


def _king_blast_ep_mask(king_blast: Mapping[str, Any]) -> bytes:
    dimensions = int(king_blast["physical_dimensions"])
    classes = tuple(king_blast["class_order"])
    bitmap = bytearray((dimensions + 7) // 8)
    for center in range(64):
        for actor_relation in range(2):
            for class_id, class_name in enumerate(classes):
                reachable = False
                if class_name == "ENEMY_KING_CENTER":
                    reachable = actor_relation == 0 or _file(center) >= 4
                elif class_name == "EN_PASSANT_MARKER":
                    reachable = _rank(center) == (5 if actor_relation == 0 else 2)
                else:
                    if class_name.startswith("ENEMY_KING_"):
                        direction = class_name[len("ENEMY_KING_") :]
                        perspective_king = actor_relation == 1
                    elif class_name.startswith("OWN_KING_"):
                        direction = class_name[len("OWN_KING_") :]
                        perspective_king = actor_relation == 0
                    else:
                        raise OracleError("feature_schema.KingBlastEP: unknown class")
                    related = _neighbor(center, direction)
                    reachable = related is not None and (
                        not perspective_king or _file(related) >= 4
                    )
                if reachable:
                    index = (center * 2 + actor_relation) * len(classes) + class_id
                    _bit_set(bitmap, index)
    return bytes(bitmap)


def _blast_ring_mask(ring: Mapping[str, Any]) -> bytes:
    dimensions = int(ring["physical_dimensions"])
    directions = tuple(ring["offset_order"])
    classes = tuple(ring["class_order"])
    bitmap = bytearray((dimensions + 7) // 8)
    for center in range(64):
        for actor_relation in range(2):
            for collateral_relation in range(2):
                for direction_id, direction in enumerate(directions):
                    collateral = _neighbor(center, direction)
                    for class_id, class_name in enumerate(classes):
                        reachable = collateral is not None
                        if (
                            reachable
                            and class_name == "ADJACENT_PAWN_SURVIVES"
                            and _rank(int(collateral)) in (0, 7)
                        ):
                            reachable = False
                        if reachable:
                            index = (
                                ((((center * 2 + actor_relation) * 2 + collateral_relation) * 8 + direction_id) * 5)
                                + class_id
                            )
                            _bit_set(bitmap, index)
    return bytes(bitmap)


def derive_hm_masks(physical: bytes) -> Tuple[bytes, bytes]:
    """Project physical HM reachability to factorized training domains."""

    if len(physical) != 2_816:
        raise OracleError("physical HM mask must contain exactly 2,816 bytes")
    training = bytearray(24_576 // 8)
    virtual = bytearray(768 // 8)
    for physical_index in range(22_528):
        if not _bit(physical, physical_index):
            continue
        bucket, remainder = divmod(physical_index, 11 * 64)
        physical_plane, square = divmod(remainder, 64)
        training_plane = physical_plane
        if physical_plane == 10:
            training_plane = 10 if square == _own_king_square(bucket) else 11
        training_index = bucket * 12 * 64 + training_plane * 64 + square
        virtual_index = training_plane * 64 + square
        _bit_set(training, training_index)
        _bit_set(virtual, virtual_index)
    return bytes(training), bytes(virtual)


def _mask_digest(
    feature_schema_sha256: str,
    perspective_id: int,
    kind_id: int,
    slice_id: int,
    dimensions: int,
    mask: bytes,
) -> str:
    payload = (
        MASK_DOMAIN
        + bytes.fromhex(feature_schema_sha256)
        + bytes((perspective_id, kind_id, slice_id))
        + dimensions.to_bytes(4, "little")
        + len(mask).to_bytes(4, "little")
        + mask
    )
    return hashlib.sha256(payload).hexdigest()


def generate_oracle_result(feature_schema: Path) -> OracleResult:
    _schema, slices, snapshot = _authenticate_feature_schema(feature_schema)
    physical_masks = (
        _hm_mask(slices[SLICE_IDS[0]]),
        _capture_pair_mask(slices[SLICE_IDS[1]]),
        _king_blast_ep_mask(slices[SLICE_IDS[2]]),
        _blast_ring_mask(slices[SLICE_IDS[3]]),
    )
    for expected, mask in zip(EXPECTED_LAYOUT, physical_masks):
        if len(mask) != expected[3]:
            raise OracleError("{}: generated bitmap size differs".format(expected[0]))

    output_parts = []
    roles: Dict[str, Dict[str, Mapping[str, Any]]] = {}
    physical_layout = []
    all_digests = []
    output_offset = 0
    for perspective_id, perspective in enumerate(PERSPECTIVES):
        role: Dict[str, Mapping[str, Any]] = {}
        for slice_id, ((identifier, _physical_offset, dimensions, byte_count), mask) in enumerate(
            zip(EXPECTED_LAYOUT, physical_masks)
        ):
            digest = _mask_digest(
                snapshot.sha256,
                perspective_id,
                MASK_KIND_IDS["physical"],
                slice_id,
                dimensions,
                mask,
            )
            field = identifier.replace("-", "_")
            role[field] = {"indices": dimensions, "bytes": byte_count, "sha256": digest}
            physical_layout.append(
                {
                    "perspective": perspective,
                    "slice": identifier,
                    "offset": output_offset,
                    "indices": dimensions,
                    "bytes": byte_count,
                    "reachable_indices": _popcount(mask),
                    "sha256": digest,
                }
            )
            output_parts.append(mask)
            output_offset += len(mask)

        training, virtual = derive_hm_masks(physical_masks[0])
        for field, kind, dimensions, mask in (
            ("hm_training", "training", 24_576, training),
            ("hm_virtual_factors", "virtual-factor", 768, virtual),
        ):
            digest = _mask_digest(
                snapshot.sha256,
                perspective_id,
                MASK_KIND_IDS[kind],
                0,
                dimensions,
                mask,
            )
            role[field] = {"indices": dimensions, "bytes": len(mask), "sha256": digest}
        roles[perspective] = role
        all_digests.extend(bytes.fromhex(role[field]["sha256"]) for field in MASK_FIELDS + DERIVED_FIELDS)

    output = b"".join(output_parts)
    if len(output) != OUTPUT_BYTES or output_offset != OUTPUT_BYTES:
        raise OracleError("oracle output does not contain exactly 18,772 bytes")
    aggregate = hashlib.sha256(MASK_AGGREGATE_DOMAIN + b"".join(all_digests)).hexdigest()
    return OracleResult(
        feature_schema_file=feature_schema.name,
        feature_schema_bytes=snapshot.byte_count,
        feature_schema_sha256=snapshot.sha256,
        output=output,
        roles=roles,
        aggregate_sha256=aggregate,
        physical_layout=tuple(physical_layout),
    )


def build_manifest(result: OracleResult, output_file: str) -> Mapping[str, Any]:
    return {
        "schema_version": 1,
        "schema_id": MANIFEST_SCHEMA_ID,
        "algorithm_version": ALGORITHM_VERSION,
        "source_contract": {
            "repository": UPSTREAM_REPOSITORY,
            "commit": UPSTREAM_CONTRACT_COMMIT,
            "feature_schema_sha256": FEATURE_SCHEMA_SHA256,
            "reachability_attestation_schema_sha256": REACHABILITY_SCHEMA_SHA256,
        },
        "feature_schema": {
            "file": result.feature_schema_file,
            "bytes": str(result.feature_schema_bytes),
            "sha256": result.feature_schema_sha256,
        },
        "oracle_output": {
            "file": output_file,
            "bytes": str(len(result.output)),
            "sha256": hashlib.sha256(result.output).hexdigest(),
        },
        "physical_layout": list(result.physical_layout),
        "roles": result.roles,
        "reachability_mask_sha256": result.aggregate_sha256,
        "verification": {
            "feature_schema_authenticated": True,
            "schema_semantics_validated": True,
            "oracle_used_no_dataset_artifacts": True,
            "all_physical_masks_reproduced": True,
            "hm_training_projection_recomputed": True,
            "hm_virtual_projection_recomputed": True,
            "aggregate_hash_recomputed": True,
            "strict_eof": True,
        },
    }


def _directory_identity(metadata: os.stat_result) -> Tuple[int, int]:
    return int(metadata.st_dev), int(metadata.st_ino)


def _assert_parent_identity(
    parent: Path, expected: Tuple[int, int], label: str
) -> None:
    try:
        metadata = os.lstat(parent)
    except OSError as exc:
        raise OracleError(label + ": output parent changed: " + str(exc)) from exc
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or _is_reparse_point(metadata)
        or _directory_identity(metadata) != expected
    ):
        raise OracleError(label + ": output parent identity changed")


def _assert_output_path(path: Path, label: str) -> Tuple[int, int]:
    _safe_basename(path.name, label)
    parent = path.parent if str(path.parent) else Path(".")
    _assert_no_link(parent, label + " parent")
    try:
        metadata = os.stat(parent, follow_symlinks=False)
    except OSError as exc:
        raise OracleError(label + ": parent is unavailable: " + str(exc)) from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise OracleError(label + ": parent must be a directory")
    if os.path.lexists(path):
        raise OracleError(label + ": refusing to overwrite existing path")
    return _directory_identity(metadata)


def _write_temp(
    parent: Path,
    basename: str,
    payload: bytes,
    parent_identity: Tuple[int, int],
    label: str,
) -> Path:
    _assert_parent_identity(parent, parent_identity, label)
    descriptor, name = tempfile.mkstemp(prefix="." + basename + ".", suffix=".tmp", dir=parent)
    path = Path(name)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            written = stream.write(payload)
            if written != len(payload):
                raise OracleError("short write while preparing transactional output")
            stream.flush()
            os.fsync(stream.fileno())
        _assert_parent_identity(parent, parent_identity, label)
        return path
    except Exception:
        try:
            path.unlink()
        except OSError:
            pass
        raise


def _link_no_replace(source: Path, destination: Path) -> None:
    try:
        os.link(source, destination)
    except FileExistsError as exc:
        raise OracleError(str(destination) + ": destination appeared during transaction") from exc
    except OSError as exc:
        raise OracleError(str(destination) + ": atomic no-replace publication failed: " + str(exc)) from exc


def _same_file(left: Path, right: Path) -> bool:
    try:
        return _identity(os.stat(left, follow_symlinks=False))[:2] == _identity(
            os.stat(right, follow_symlinks=False)
        )[:2]
    except OSError:
        return False


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish_pair(
    first_path: Path, first_payload: bytes, second_path: Path, second_payload: bytes
) -> None:
    if os.path.normcase(os.path.abspath(first_path)) == os.path.normcase(
        os.path.abspath(second_path)
    ):
        raise OracleError("output and manifest paths must be distinct")
    first_parent_identity = _assert_output_path(first_path, "output")
    second_parent_identity = _assert_output_path(second_path, "manifest")
    first_parent = first_path.parent if str(first_path.parent) else Path(".")
    second_parent = second_path.parent if str(second_path.parent) else Path(".")
    first_temp: Path | None = None
    second_temp: Path | None = None
    first_published = False
    second_published = False
    try:
        first_temp = _write_temp(
            first_parent,
            first_path.name,
            first_payload,
            first_parent_identity,
            "output",
        )
        second_temp = _write_temp(
            second_parent,
            second_path.name,
            second_payload,
            second_parent_identity,
            "manifest",
        )
        _assert_parent_identity(first_parent, first_parent_identity, "output")
        _link_no_replace(first_temp, first_path)
        first_published = True
        _assert_parent_identity(first_parent, first_parent_identity, "output")
        _assert_parent_identity(second_parent, second_parent_identity, "manifest")
        _link_no_replace(second_temp, second_path)
        second_published = True
        _assert_parent_identity(second_parent, second_parent_identity, "manifest")
        if not _same_file(first_temp, first_path) or not _same_file(
            second_temp, second_path
        ):
            raise OracleError("published output identity changed during transaction")
        _fsync_directory(first_parent)
        if second_parent.resolve() != first_parent.resolve():
            _fsync_directory(second_parent)
    except Exception:
        if (
            second_published
            and second_temp is not None
            and _same_file(second_temp, second_path)
        ):
            try:
                second_path.unlink()
            except OSError:
                pass
        if (
            first_published
            and first_temp is not None
            and _same_file(first_temp, first_path)
        ):
            try:
                first_path.unlink()
            except OSError:
                pass
        raise
    finally:
        for temporary in (first_temp, second_temp):
            if temporary is None:
                continue
            try:
                temporary.unlink()
            except OSError:
                pass


def _publish_one(path: Path, payload: bytes, label: str) -> None:
    parent_identity = _assert_output_path(path, label)
    parent = path.parent if str(path.parent) else Path(".")
    temporary = _write_temp(parent, path.name, payload, parent_identity, label)
    published = False
    try:
        _assert_parent_identity(parent, parent_identity, label)
        _link_no_replace(temporary, path)
        published = True
        _assert_parent_identity(parent, parent_identity, label)
        if not _same_file(temporary, path):
            raise OracleError(label + ": published identity changed during transaction")
        _fsync_directory(parent)
    except Exception:
        if published and _same_file(temporary, path):
            try:
                path.unlink()
            except OSError:
                pass
        raise
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass


def _manifest_keys() -> Tuple[str, ...]:
    return (
        "schema_version",
        "schema_id",
        "algorithm_version",
        "source_contract",
        "feature_schema",
        "oracle_output",
        "physical_layout",
        "roles",
        "reachability_mask_sha256",
        "verification",
    )


def _validate_manifest(
    manifest: Mapping[str, Any], result: OracleResult, output_path: Path
) -> None:
    _require_keys(manifest, _manifest_keys(), "manifest")
    expected = build_manifest(result, output_path.name)
    if manifest != expected:
        raise OracleError("manifest: content does not match recomputed oracle evidence")


def _validate_controller(
    value: Mapping[str, Any], oracle_binary: FileSnapshot, oracle_binary_name: str
) -> Tuple[Mapping[str, Any], Mapping[str, Any], Mapping[str, Any]]:
    _require_keys(
        value,
        (
            "schema_version",
            "schema_id",
            "campaign",
            "producer_attestation",
            "coverage_policy",
            "oracle",
            "verification",
        ),
        "controller",
    )
    if value.get("schema_version") != 1 or value.get("schema_id") != CONTROLLER_SCHEMA_ID:
        raise OracleError("controller: unsupported schema identity")
    campaign = _artifact_descriptor(value.get("campaign"), "controller.campaign", True)
    producer = _artifact_descriptor(
        value.get("producer_attestation"), "controller.producer_attestation", True
    )
    coverage = _artifact_descriptor(
        value.get("coverage_policy"), "controller.coverage_policy", True
    )
    if campaign["schema_sha256"] != CAMPAIGN_SCHEMA_SHA256:
        raise OracleError("controller.campaign: schema SHA-256 mismatch")
    if producer["schema_sha256"] != PRODUCER_SCHEMA_SHA256:
        raise OracleError("controller.producer_attestation: schema SHA-256 mismatch")
    if coverage["schema_sha256"] != COVERAGE_POLICY_SCHEMA_SHA256:
        raise OracleError("controller.coverage_policy: schema SHA-256 mismatch")
    oracle = _mapping(value.get("oracle"), "controller.oracle")
    _require_keys(oracle, ("commit", "binary"), "controller.oracle")
    if not isinstance(oracle.get("commit"), str) or COMMIT_RE.fullmatch(oracle["commit"]) is None:
        raise OracleError("controller.oracle.commit: must be lowercase 40-hex")
    binary = _artifact_descriptor(oracle.get("binary"), "controller.oracle.binary", False)
    if binary["file"] != oracle_binary_name:
        raise OracleError("controller.oracle.binary.file: does not name --oracle-binary")
    if binary["bytes"] != str(oracle_binary.byte_count) or binary["sha256"] != oracle_binary.sha256:
        raise OracleError("controller.oracle.binary: authenticated descriptor mismatch")
    verification = _mapping(value.get("verification"), "controller.verification")
    _require_keys(verification, CONTROLLER_VERIFICATION_FIELDS, "controller.verification")
    for field in CONTROLLER_VERIFICATION_FIELDS:
        if verification.get(field) is not True:
            raise OracleError("controller.verification.{}: must be true".format(field))
    return campaign, producer, coverage


def _verify_coverage_policy(
    path: Path,
    descriptor: Mapping[str, Any],
    result: OracleResult,
) -> None:
    policy, snapshot = _load_json_snapshot(path, "coverage_policy", canonical=True)
    if descriptor["file"] != path.name:
        raise OracleError("controller.coverage_policy.file: does not name --coverage-policy")
    if descriptor["bytes"] != str(snapshot.byte_count) or descriptor["sha256"] != snapshot.sha256:
        raise OracleError("controller.coverage_policy: authenticated descriptor mismatch")
    if policy.get("reachability_mask_sha256") != result.aggregate_sha256:
        raise OracleError("coverage_policy: reachability aggregate differs from oracle")
    expected_masks = {
        perspective: {
            field: result.roles[perspective][field]["sha256"]
            for field in MASK_FIELDS + DERIVED_FIELDS
        }
        for perspective in PERSPECTIVES
    }
    if policy.get("reachability_masks") != expected_masks:
        raise OracleError("coverage_policy: twelve reachability hashes differ from oracle")


def _artifact_digest(value: Mapping[str, Any]) -> bytes:
    return bytes.fromhex(str(value["sha256"]))


def _attestation_evidence_sha256(attestation: Mapping[str, Any]) -> str:
    payload = bytearray(ATTESTATION_DOMAIN)
    payload += _artifact_digest(attestation["campaign"])
    payload += _artifact_digest(attestation["producer_attestation"])
    payload += _artifact_digest(attestation["feature_schema"])
    oracle = attestation["oracle"]
    payload += bytes.fromhex(oracle["commit"])
    payload += _artifact_digest(oracle["binary"])
    algorithm = oracle["algorithm_version"].encode("ascii")
    payload += len(algorithm).to_bytes(2, "little") + algorithm
    payload += _artifact_digest(attestation["oracle_output"])
    for perspective in PERSPECTIVES:
        for field in MASK_FIELDS + DERIVED_FIELDS:
            payload += bytes.fromhex(attestation["roles"][perspective][field]["sha256"])
    payload += bytes.fromhex(attestation["reachability_mask_sha256"])
    for field in ATTESTATION_VERIFICATION_FIELDS:
        payload.append(int(attestation["verification"][field]))
    return hashlib.sha256(payload).hexdigest()


def build_attestation(
    result: OracleResult,
    output_path: Path,
    campaign: Mapping[str, Any],
    producer: Mapping[str, Any],
    oracle_commit: str,
    oracle_binary_name: str,
    oracle_binary: FileSnapshot,
) -> Mapping[str, Any]:
    attestation: MutableMapping[str, Any] = {
        "schema_version": 1,
        "campaign": campaign,
        "producer_attestation": producer,
        "feature_schema": {
            "file": result.feature_schema_file,
            "bytes": str(result.feature_schema_bytes),
            "sha256": result.feature_schema_sha256,
        },
        "oracle": {
            "commit": oracle_commit,
            "binary": {
                "file": oracle_binary_name,
                "bytes": str(oracle_binary.byte_count),
                "sha256": oracle_binary.sha256,
            },
            "algorithm_version": ALGORITHM_VERSION,
        },
        "oracle_output": {
            "file": output_path.name,
            "bytes": str(len(result.output)),
            "sha256": hashlib.sha256(result.output).hexdigest(),
        },
        "roles": result.roles,
        "reachability_mask_sha256": result.aggregate_sha256,
        "verification": {field: True for field in ATTESTATION_VERIFICATION_FIELDS},
        "evidence_sha256": "0" * 64,
    }
    attestation["evidence_sha256"] = _attestation_evidence_sha256(attestation)
    return attestation


def _capabilities() -> Mapping[str, Any]:
    return {
        "schema_version": 1,
        "algorithm_version": ALGORITHM_VERSION,
        "feature_schema_sha256": FEATURE_SCHEMA_SHA256,
        "upstream_contract_commit": UPSTREAM_CONTRACT_COMMIT,
        "physical_mask_count": 8,
        "oracle_output_bytes": OUTPUT_BYTES,
        "stdlib_only": True,
        "dataset_inputs_accepted": False,
    }


def _generate(args: argparse.Namespace) -> Mapping[str, Any]:
    feature_path = Path(args.feature_schema)
    output_path = Path(args.output)
    manifest_path = Path(args.manifest)
    result = generate_oracle_result(feature_path)
    manifest = build_manifest(result, output_path.name)
    manifest_payload = _canonical_json(manifest)
    _publish_pair(output_path, result.output, manifest_path, manifest_payload)
    return {
        "ok": True,
        "output": output_path.name,
        "bytes": len(result.output),
        "sha256": hashlib.sha256(result.output).hexdigest(),
        "manifest": manifest_path.name,
        "manifest_sha256": hashlib.sha256(manifest_payload).hexdigest(),
        "reachability_mask_sha256": result.aggregate_sha256,
    }


def _attest(args: argparse.Namespace) -> Mapping[str, Any]:
    feature_path = Path(args.feature_schema)
    output_path = Path(args.oracle_output)
    manifest_path = Path(args.manifest)
    controller_path = Path(args.controller_descriptors)
    policy_path = Path(args.coverage_policy)
    oracle_binary_path = Path(args.oracle_binary)
    attestation_path = Path(args.attestation)

    result = generate_oracle_result(feature_path)
    output_snapshot = _read_regular_snapshot(output_path, OUTPUT_BYTES, "oracle_output")
    if output_snapshot.payload != result.output:
        raise OracleError("oracle_output: bytes differ from fresh symbolic reproduction")
    manifest, _manifest_snapshot = _load_json_snapshot(
        manifest_path, "manifest", canonical=True
    )
    _validate_manifest(manifest, result, output_path)
    oracle_binary = _read_regular_snapshot(
        oracle_binary_path, MAX_JSON_BYTES, "oracle_binary"
    )
    controller, controller_snapshot = _load_json_snapshot(
        controller_path, "controller", canonical=True
    )
    expected_controller_sha256 = _sha256(
        args.expected_controller_sha256, "expected_controller_sha256"
    )
    if controller_snapshot.sha256 != expected_controller_sha256:
        raise OracleError(
            "controller: SHA-256 differs from the external controller trust anchor"
        )
    campaign, producer, coverage = _validate_controller(
        controller, oracle_binary, oracle_binary_path.name
    )
    _verify_coverage_policy(policy_path, coverage, result)
    attestation = build_attestation(
        result,
        output_path,
        campaign,
        producer,
        controller["oracle"]["commit"],
        oracle_binary_path.name,
        oracle_binary,
    )
    payload = _canonical_json(attestation)
    _publish_one(attestation_path, payload, "attestation")
    return {
        "ok": True,
        "attestation": attestation_path.name,
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "evidence_sha256": attestation["evidence_sha256"],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Independent AtomicNNUEV3 symbolic reachability oracle"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("capabilities", help="print the immutable oracle contract")

    generate = subparsers.add_parser(
        "generate", help="transactionally publish the physical masks and manifest"
    )
    generate.add_argument("--feature-schema", required=True)
    generate.add_argument("--output", required=True)
    generate.add_argument("--manifest", required=True)

    attest = subparsers.add_parser(
        "attest", help="compose H9.3l-a evidence from controller-authenticated descriptors"
    )
    attest.add_argument("--feature-schema", required=True)
    attest.add_argument("--oracle-output", required=True)
    attest.add_argument("--manifest", required=True)
    attest.add_argument("--controller-descriptors", required=True)
    attest.add_argument("--expected-controller-sha256", required=True)
    attest.add_argument("--coverage-policy", required=True)
    attest.add_argument("--oracle-binary", required=True)
    attest.add_argument("--attestation", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "capabilities":
            result = _capabilities()
        elif args.command == "generate":
            result = _generate(args)
        elif args.command == "attest":
            result = _attest(args)
        else:  # pragma: no cover - argparse owns command dispatch
            raise OracleError("unsupported command")
    except (OracleError, OSError, KeyError, TypeError, ValueError, OverflowError) as exc:
        print("atomic-v3-reachability-oracle: " + str(exc), file=sys.stderr)
        return 1
    sys.stdout.buffer.write(_canonical_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
