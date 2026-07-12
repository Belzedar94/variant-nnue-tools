#!/usr/bin/env python3
"""Cross-component gate for the pinned Atomic generator and tools backend."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import subprocess
import tempfile
from typing import Sequence


GENERATOR_SCHEMA = (
    '{"schema_sha256":"acca0f551f1c012c31a6c727dedccaebb7b5ebbc46810edb87e31bb208d5abe1",'
    '"formats":{"legacy-atomic-v1":{"read":false,"write":true,"record_size":72}}}'
)
TOOLS_SCHEMA = (
    '{"schema_sha256":"acca0f551f1c012c31a6c727dedccaebb7b5ebbc46810edb87e31bb208d5abe1",'
    '"formats":{"legacy-atomic-v1":{"read":true,"write":true,"record_size":72}}}'
)
RECORD_SIZE = 72
RESOLVED_SEED = 4843478989694531390
EXPECTED_DATA_BY_NET = {
    "9CF054CA00B82AB53A34473DE52D1104AEDDAA19B2E7B24091B5E613AF485985": (
        "762555D8C054B8CED4FE1A18397711F2E6E10EB55397EA242DC9479BBC1F339A"
    ),
    "99DC67EABF26A64FAEECA3A88B4C38597A840B8D4A874B9F2CF658C6F92A04A6": (
        "7E89411B84C2036DEEB2DB56F3E43FEA89917C5546C72C37F8E082F103B27CC0"
    ),
}


def require_file(path: Path, label: str) -> Path:
    resolved = path.expanduser().resolve()
    if not resolved.is_file():
        raise AssertionError(f"{label} does not exist: {resolved}")
    return resolved


def run_binary(
    binary: Path,
    commands: Sequence[str],
    *,
    prefix: Sequence[str] = (),
    expect_success: bool = True,
    timeout: float = 120.0,
) -> str:
    completed = subprocess.run(
        [*prefix, str(binary)],
        input="\n".join((*commands, "")),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )
    output = completed.stdout + completed.stderr
    if expect_success and completed.returncode != 0:
        raise AssertionError(
            f"{binary.name} failed with exit code {completed.returncode}:\n{output}"
        )
    if not expect_success and completed.returncode == 0:
        raise AssertionError(f"{binary.name} unexpectedly succeeded:\n{output}")
    return output


def generator_command(output: Path) -> str:
    return (
        "generate_training_data depth 1 count 2 write_min_ply 0 write_max_ply 2 "
        "random_move_count 0 keep_draws 1 eval_limit 32000 "
        "filter_captures false filter_checks false filter_promotions false "
        f"output_file_name {output} data_format bin seed tools-wire-test"
    )


def generate(
    generator: Path,
    net: Path,
    output: Path,
    *,
    prefix: Sequence[str] = (),
    timeout: float = 120.0,
) -> str:
    return run_binary(
        generator,
        (
            "uci",
            f"setoption name EvalFile value {net}",
            "setoption name Use NNUE value pure",
            "setoption name Threads value 1",
            "setoption name Hash value 16",
            "isready",
            "atomic_data_schema",
            generator_command(output),
            "quit",
        ),
        prefix=prefix,
        timeout=timeout,
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--generator", type=Path, required=True)
    parser.add_argument("--tools", type=Path, required=True)
    parser.add_argument("--net", type=Path, required=True)
    parser.add_argument("--expected-data-sha256")
    parser.add_argument(
        "--valgrind",
        action="store_true",
        help="run both native components under strict Valgrind Memcheck",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    generator = require_file(args.generator, "Atomic data generator")
    tools = require_file(args.tools, "Atomic data tools")
    net = require_file(args.net, "Atomic NNUE")
    net_sha = hashlib.sha256(net.read_bytes()).hexdigest().upper()
    expected_data_sha = (
        args.expected_data_sha256.upper()
        if args.expected_data_sha256
        else EXPECTED_DATA_BY_NET.get(net_sha)
    )
    if expected_data_sha is None:
        raise AssertionError(
            "unknown network fixture; pass --expected-data-sha256 explicitly "
            f"(network SHA-256 {net_sha})"
        )

    prefix: tuple[str, ...] = ()
    timeout = 120.0
    if args.valgrind:
        prefix = (
            "valgrind",
            "--error-exitcode=42",
            "--leak-check=full",
            "--show-leak-kinds=all",
            "--errors-for-leak-kinds=all",
            "--track-origins=yes",
        )
        timeout = 300.0

    with tempfile.TemporaryDirectory(prefix="atomic-wrapper-") as raw_root:
        root = Path(raw_root).resolve()
        if any(character.isspace() for character in str(root)):
            raise AssertionError(
                f"generator command paths must not contain whitespace: {root}"
            )
        first = root / "first.bin"
        second = root / "second.bin"
        removed = root / "removed-tools-generator.bin"

        first_output = generate(
            generator, net, first, prefix=prefix, timeout=timeout
        )
        if first_output.splitlines().count(GENERATOR_SCHEMA) != 1:
            raise AssertionError(f"generator schema handshake mismatch:\n{first_output}")
        if first_output.splitlines().count(
            f"PRNG::initial_seed = {RESOLVED_SEED}"
        ) != 1:
            raise AssertionError(f"generator seed marker mismatch:\n{first_output}")
        if first_output.splitlines().count("INFO: generate_training_data finished.") != 1:
            raise AssertionError(f"generator completion marker mismatch:\n{first_output}")

        data = first.read_bytes()
        if len(data) != 2 * RECORD_SIZE:
            raise AssertionError(f"expected 144 generated bytes, got {len(data)}")
        if any(data[offset + 71] != 0 for offset in range(0, len(data), RECORD_SIZE)):
            raise AssertionError("generated Legacy V1 padding is not zero")
        actual_data_sha = hashlib.sha256(data).hexdigest().upper()
        if actual_data_sha != expected_data_sha:
            raise AssertionError(
                f"generated data hash mismatch: {actual_data_sha} != {expected_data_sha}"
            )

        generate(generator, net, second, prefix=prefix, timeout=timeout)
        if second.read_bytes() != data:
            raise AssertionError("Atomic generator replay is not byte-exact")

        tools_output = run_binary(
            tools,
            (
                "uci",
                "setoption name UCI_Variant value atomic",
                "setoption name Threads value 1",
                "setoption name Use NNUE value false",
                "atomic_data_schema",
                f"validate_training_data {first}",
                generator_command(removed),
                "quit",
            ),
            prefix=prefix,
            timeout=timeout,
        )
        if tools_output.splitlines().count(TOOLS_SCHEMA) != 1:
            raise AssertionError(f"tools schema handshake mismatch:\n{tools_output}")
        if (
            "Validation passed: 2 canonical legacy v1 records (72 bytes each)."
            not in tools_output
        ):
            raise AssertionError(f"tools did not validate generated data:\n{tools_output}")
        if "Unknown command: generate_training_data" not in tools_output:
            raise AssertionError(f"tools still exposes the removed PV generator:\n{tools_output}")
        if "INFO: generate_training_data finished." in tools_output or removed.exists():
            raise AssertionError("tools backend executed the removed PV generator")

    print(
        "Atomic wrapper integration passed "
        f"records=2 data_sha256={actual_data_sha} net_sha256={net_sha}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
