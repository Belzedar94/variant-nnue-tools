#!/usr/bin/env python3

import argparse
import hashlib
from pathlib import Path
import struct
import subprocess
import tempfile


RECORD_SIZE = 72
PACKED_SFEN_SIZE = 64
MOVE_OFFSET = 66
PLY_OFFSET = 68
RESULT_OFFSET = 70
PADDING_OFFSET = 71
REFUSAL_TEXT = "the path already exists; choose a new output name or remove it explicitly"
SPECIAL_MOVES_SHA256 = "C8F5C7FEB92C5F10B3CC2C37E2685A6E9993C486E335BBD7EAA38C22B229B2AA"
ATOMIC_DATA_SCHEMA_JSON = (
    '{"schema_sha256":"acca0f551f1c012c31a6c727dedccaebb7b5ebbc46810edb87e31bb208d5abe1",'
    '"formats":{"legacy-atomic-v1":{"read":true,"write":true,"record_size":72}}}'
)


def run_engine(
    engine,
    command,
    expect_success=True,
    timeout=60,
    failure_text=REFUSAL_TEXT,
    extra_commands=None,
    variant="atomic",
):
    if extra_commands is None:
        extra_commands = []
    setup_commands = ["uci"]
    if variant is not None:
        setup_commands.append("setoption name UCI_Variant value {}".format(variant))
    setup_commands.extend(
        [
            "setoption name Threads value 1",
            "setoption name Use NNUE value false",
        ]
    )
    commands = "\n".join(
        [
            *setup_commands,
            *extra_commands,
            command,
            "quit",
            "",
        ]
    )
    result = subprocess.run(
        [str(engine)],
        input=commands,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )
    output = result.stdout + result.stderr

    if expect_success and result.returncode != 0:
        raise AssertionError(
            "engine command failed with exit code {}:\n{}".format(result.returncode, output)
        )
    if not expect_success:
        if result.returncode == 0:
            raise AssertionError("existing output was not rejected:\n{}".format(output))
        if failure_text not in output:
            raise AssertionError("output refusal was not explicit:\n{}".format(output))

    return output


def expect_refusal_without_change(engine, command, output_path):
    before = output_path.read_bytes()
    run_engine(engine, command, expect_success=False)
    after = output_path.read_bytes()
    if after != before:
        raise AssertionError("rejected command modified existing output {}".format(output_path))


def expect_atomic_data_schema(engine):
    output = run_engine(engine, "atomic_data_schema", variant=None)
    json_lines = [line for line in output.splitlines() if line.lstrip().startswith("{")]
    if json_lines != [ATOMIC_DATA_SCHEMA_JSON]:
        raise AssertionError(
            "atomic_data_schema did not emit exactly the normative JSON line:\n{}".format(output)
        )

    option_line = "option name UCI_Variant type combo default atomic var atomic"
    if output.splitlines().count(option_line) != 1:
        raise AssertionError(
            "the transition backend is not Atomic-only by default:\n{}".format(output)
        )

    rejected = run_engine(engine, "atomic_data_schema", variant="chess")
    if "Atomic data tools only support UCI_Variant=atomic" not in rejected:
        raise AssertionError(
            "the transition backend did not reject UCI_Variant=chess:\n{}".format(
                rejected
            )
        )
    rejected_json = [
        line for line in rejected.splitlines() if line.lstrip().startswith("{")
    ]
    if rejected_json != [ATOMIC_DATA_SCHEMA_JSON]:
        raise AssertionError(
            "a rejected variant change escaped the Atomic schema:\n{}".format(rejected)
        )

    rejected_config = run_engine(
        engine,
        "atomic_data_schema",
        variant=None,
        extra_commands=["setoption name VariantPath value forbidden.ini"],
    )
    if "Atomic data tools reject runtime variant definitions" not in rejected_config:
        raise AssertionError(
            "the transition backend accepted a mutable variant path:\n{}".format(
                rejected_config
            )
        )


def expect_pv_generator_absent(engine, output_path):
    output = run_engine(
        engine,
        "generate_training_data depth 1 count 1 output_file_name {} data_format bin".format(
            output_path
        ),
    )
    if "Unknown command: generate_training_data" not in output:
        raise AssertionError(
            "the temporary tools backend still exposes PV generation:\n{}".format(output)
        )
    if output_path.exists():
        raise AssertionError("removed PV generator created {}".format(output_path))


def expect_validation_failure(engine, path, reason_fragment=None, extra_commands=None):
    output = run_engine(
        engine,
        "validate_training_data {}".format(path),
        expect_success=False,
        failure_text="Validation failed",
        extra_commands=extra_commands,
    )
    if reason_fragment is not None and reason_fragment not in output:
        raise AssertionError(
            "validation failed for the wrong reason (expected {!r}):\n{}".format(
                reason_fragment, output
            )
        )


def record_field(data, record_index, offset, fmt):
    return struct.unpack_from(fmt, data, record_index * RECORD_SIZE + offset)[0]


def packed_bit(data, bit):
    return (data[bit // 8] >> (bit % 8)) & 1


def read_lsb_bits(data, start, width):
    return sum(packed_bit(data, start + offset) << offset for offset in range(width))


def write_lsb_bits(data, start, width, value):
    for offset in range(width):
        bit = start + offset
        mask = 1 << (bit % 8)
        if value & (1 << offset):
            data[bit // 8] |= mask
        else:
            data[bit // 8] &= ~mask


def square_index(name):
    if len(name) != 2 or name[0] not in "abcdefgh" or name[1] not in "12345678":
        raise ValueError("invalid 8x8 square: {!r}".format(name))
    return ord(name[0]) - ord("a") + (int(name[1]) - 1) * 8


def discover_ep_flag(with_ep, without_ep, expected_target):
    if len(with_ep) != RECORD_SIZE or len(without_ep) != RECORD_SIZE:
        raise AssertionError("EP discovery requires two single-record datasets")
    ep_flag_bit = next(
        (
            bit
            for bit in range(PACKED_SFEN_SIZE * 8)
            if packed_bit(with_ep, bit) != packed_bit(without_ep, bit)
        ),
        None,
    )
    if ep_flag_bit is None:
        raise AssertionError("packed EP and no-EP records were identical")
    if not packed_bit(with_ep, ep_flag_bit) or packed_bit(without_ep, ep_flag_bit):
        raise AssertionError("first packed difference was not the EP-present bit")
    if read_lsb_bits(with_ep, ep_flag_bit + 1, 7) != square_index(expected_target):
        raise AssertionError(
            "discovered packed EP field did not encode {}".format(expected_target)
        )
    return ep_flag_bit


def main():
    parser = argparse.ArgumentParser(description="End-to-end tests for the legacy 72-byte data wire")
    parser.add_argument("--engine", required=True, help="Path to the built Fairy-Stockfish tools binary")
    args = parser.parse_args()

    engine = Path(args.engine).resolve()
    if not engine.is_file():
        raise AssertionError("engine does not exist: {}".format(engine))
    expect_atomic_data_schema(engine)

    with tempfile.TemporaryDirectory(prefix="variant-nnue-tools-") as temp_name:
        root = Path(temp_name)
        if any(character.isspace() for character in str(root)):
            raise AssertionError("the engine command parser requires a whitespace-free temp path")

        plain = root / "special-moves.txt"
        binary = root / "special-moves.bin"
        exported_plain = root / "exported.plain"
        exported_epd = root / "exported.epd"
        roundtrip = root / "roundtrip.bin"
        roundtrip_plain = root / "roundtrip.txt"
        empty_pgn = root / "empty.pgn"
        pgn_binary = root / "empty-pgn.bin"
        removed_pv_output = root / "removed-pv-generator.bin"
        invalid_nonpv_output = root / "invalid-nonpv-zero-ply.bin"
        invalid_nonpv_rate_output = root / "invalid-nonpv-zero-rate.bin"
        filtered_plain = root / "filtered.plain"
        filtered_binary = root / "filtered.bin"
        stats_output = root / "stats.txt"
        standard_ep_plain = root / "standard-ep.plain"
        standard_ep_binary = root / "standard-ep.bin"
        inconsistent_ep_plain = root / "inconsistent-ep.plain"
        inconsistent_ep_binary = root / "inconsistent-ep.bin"
        packed_ep_plain = root / "packed-ep.plain"
        packed_no_ep_plain = root / "packed-no-ep.plain"
        packed_no_ep_black_pawn_plain = root / "packed-no-ep-black-pawn.plain"
        packed_ep_binary = root / "packed-ep.bin"
        packed_no_ep_binary = root / "packed-no-ep.bin"
        packed_no_ep_black_pawn_binary = root / "packed-no-ep-black-pawn.bin"
        inconsistent_packed_ep_binary = root / "inconsistent-packed-ep.bin"
        no_capturer_packed_ep_binary = root / "no-capturer-packed-ep.bin"
        illegal_move_plain = root / "illegal-move.plain"
        unchecked_illegal_binary = root / "unchecked-illegal.bin"
        checked_illegal_binary = root / "checked-illegal.bin"

        # The second record deliberately omits score/ply/result. It must receive
        # the format defaults rather than inheriting values from the first one.
        plain.write_text(
            """fen rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1
move e2e4
score 42
ply 321
result 1
e
fen r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1
move e1g1
e
fen 4k3/8/8/3pP3/8/8/8/4K3 w - d6 0 1
move e5d6
score 0
ply 3
result 0
e
fen 4k3/P7/8/8/8/8/8/4K3 w - - 0 1
move a7a8n
score 0
ply 4
result 0
e
fen 4k3/P7/8/8/8/8/8/4K3 w - - 0 1
move a7a8b
score 0
ply 5
result 0
e
fen 4k3/P7/8/8/8/8/8/4K3 w - - 0 1
move a7a8r
score 0
ply 6
result 0
e
fen 4k3/P7/8/8/8/8/8/4K3 w - - 0 1
move a7a8q
score 0
ply 7
result 0
e
""",
            encoding="utf-8",
            newline="\n",
        )

        convert_to_bin = "convert_bin targetfile {} output_file_name {} check_illegal_move 1".format(
            plain, binary
        )
        run_engine(engine, convert_to_bin)

        data = binary.read_bytes()
        if len(data) != 7 * RECORD_SIZE:
            raise AssertionError("expected 7 records/504 bytes, got {} bytes".format(len(data)))

        expected_moves = [
            0x031C,  # e2e4, normal
            0xC107,  # e1h1 internally, castling
            0x892B,  # e5d6, en passant
            0x4C38,  # a7a8n
            0x5C38,  # a7a8b
            0x6C38,  # a7a8r
            0x7C38,  # a7a8q
        ]
        actual_moves = [record_field(data, i, MOVE_OFFSET, "<H") for i in range(7)]
        if actual_moves != expected_moves:
            raise AssertionError(
                "legacy move wire mismatch: expected {}, got {}".format(
                    [hex(value) for value in expected_moves],
                    [hex(value) for value in actual_moves],
                )
            )

        if record_field(data, 0, PLY_OFFSET, "<H") != 321:
            raise AssertionError("explicit ply was not preserved")
        if record_field(data, 0, RESULT_OFFSET, "<b") != 1:
            raise AssertionError("explicit result was not preserved")
        if record_field(data, 1, PLY_OFFSET, "<H") != 1:
            raise AssertionError("new record inherited the preceding ply")
        if record_field(data, 1, RESULT_OFFSET, "<b") != 0:
            raise AssertionError("new record inherited the preceding result")
        if any(record_field(data, i, PADDING_OFFSET, "<B") != 0 for i in range(7)):
            raise AssertionError("PackedSfenValue padding is not deterministic")

        expect_refusal_without_change(engine, convert_to_bin, binary)

        convert_to_plain = "convert_plain targetfile {} output_file_name {}".format(
            binary, exported_plain
        )
        run_engine(engine, convert_to_plain)
        expect_refusal_without_change(engine, convert_to_plain, exported_plain)

        convert_to_epd = "convert_epd targetfile {} output_file_name {}".format(binary, exported_epd)
        run_engine(engine, convert_to_epd)
        expect_refusal_without_change(engine, convert_to_epd, exported_epd)

        convert_roundtrip = "convert_bin targetfile {} output_file_name {} check_illegal_move 1".format(
            exported_plain, roundtrip
        )
        run_engine(engine, convert_roundtrip)
        roundtrip_data = roundtrip.read_bytes()
        if len(roundtrip_data) != len(data):
            raise AssertionError(
                "bin -> plain -> bin changed record count: {} vs {} bytes".format(
                    len(data), len(roundtrip_data)
                )
            )
        for record_index in range(7):
            payload_start = record_index * RECORD_SIZE + 64
            payload_end = (record_index + 1) * RECORD_SIZE
            if roundtrip_data[payload_start:payload_end] != data[payload_start:payload_end]:
                raise AssertionError(
                    "bin -> plain -> bin changed score/move/ply/result payload for record {}".format(
                        record_index
                    )
                )

        run_engine(
            engine,
            "convert_plain targetfile {} output_file_name {}".format(
                roundtrip, roundtrip_plain
            ),
        )
        if roundtrip_plain.read_text(encoding="utf-8") != exported_plain.read_text(encoding="utf-8"):
            raise AssertionError("bin -> plain -> bin changed the decoded positions or moves")

        validation_output = run_engine(
            engine, "validate_training_data {}".format(binary)
        )
        if "Validation passed: 7 canonical legacy v1 records" not in validation_output:
            raise AssertionError("binary validator did not report its record count")
        run_engine(engine, "validate_training_data {}".format(exported_plain))

        run_engine(
            engine,
            "validate_training_data {}".format(exported_plain),
            expect_success=False,
            failure_text="cannot represent Chess960 castling state",
            extra_commands=["setoption name UCI_Chess960 value true"],
        )

        truncated = root / "truncated.bin"
        truncated.write_bytes(data[:-1])
        expect_validation_failure(engine, truncated, "multiple of 72 bytes")

        empty_binary = root / "empty.bin"
        empty_binary.write_bytes(b"")
        expect_validation_failure(engine, empty_binary, "dataset is empty")

        bad_padding = root / "bad-padding.bin"
        bad_padding_data = bytearray(data)
        bad_padding_data[PADDING_OFFSET] = 1
        bad_padding.write_bytes(bad_padding_data)
        expect_validation_failure(engine, bad_padding, "padding byte is not zero")

        bad_result = root / "bad-result.bin"
        bad_result_data = bytearray(data)
        bad_result_data[RESULT_OFFSET] = 2
        bad_result.write_bytes(bad_result_data)
        expect_validation_failure(engine, bad_result, "game result is outside")

        bad_move = root / "bad-move.bin"
        bad_move_data = bytearray(data)
        struct.pack_into("<H", bad_move_data, MOVE_OFFSET, 0)
        bad_move.write_bytes(bad_move_data)
        expect_validation_failure(engine, bad_move, "move field is not canonical")

        bad_reserved_bits = root / "bad-reserved-bits.bin"
        bad_reserved_data = bytearray(data)
        bad_reserved_data[63] |= 0x80
        bad_reserved_bits.write_bytes(bad_reserved_data)
        expect_validation_failure(engine, bad_reserved_bits, "reserved bits")

        truncated_plain_output = root / "must-not-exist.plain"
        run_engine(
            engine,
            "convert_plain targetfile {} output_file_name {}".format(
                truncated, truncated_plain_output
            ),
            expect_success=False,
            failure_text="truncated or unreadable",
        )
        if truncated_plain_output.exists():
            raise AssertionError("truncated input created a partial plain output")

        filtered_plain.write_text(
            """fen rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1
move e2e4
score 1
ply 1
result 0
e
fen rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1
score 2
e
fen rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1
move e2e4
ply 70000
e
fen rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1
move e2e4
result 2
e
fen rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1
move e2e4
score nan
e
""",
            encoding="utf-8",
            newline="\n",
        )
        filter_output = run_engine(
            engine,
            "convert_bin targetfile {} output_file_name {}".format(
                filtered_plain, filtered_binary
            ),
        )
        if filtered_binary.stat().st_size != RECORD_SIZE:
            raise AssertionError("malformed plain records were not filtered")
        if "malformed/incomplete record" not in filter_output:
            raise AssertionError("converter did not report malformed record filtering")
        run_engine(engine, "validate_training_data {}".format(filtered_binary))

        standard_ep_plain.write_text(
            """fen rnbqkbnr/pppppppp/8/8/4P3/8/PPPP1PPP/RNBQKBNR b KQkq e3 0 1
move e7e5
score 0
ply 1
result 0
e
""",
            encoding="utf-8",
            newline="\n",
        )
        run_engine(
            engine, "validate_training_data {}".format(standard_ep_plain)
        )
        run_engine(
            engine,
            "convert_bin targetfile {} output_file_name {}".format(
                standard_ep_plain, standard_ep_binary
            ),
        )
        if standard_ep_binary.stat().st_size != RECORD_SIZE:
            raise AssertionError(
                "standard non-capturable en-passant FEN was filtered"
            )
        run_engine(engine, "validate_training_data {}".format(standard_ep_binary))

        inconsistent_ep_plain.write_text(
            standard_ep_plain.read_text(encoding="utf-8").replace(
                " KQkq e3 ", " KQkq d3 "
            ),
            encoding="utf-8",
            newline="\n",
        )
        expect_validation_failure(
            engine, inconsistent_ep_plain, "invalid or non-canonical FEN"
        )
        run_engine(
            engine,
            "convert_bin targetfile {} output_file_name {}".format(
                inconsistent_ep_plain, inconsistent_ep_binary
            ),
            expect_success=False,
            failure_text="produced no valid records",
        )
        if inconsistent_ep_binary.exists():
            raise AssertionError("inconsistent en-passant FEN left an output file")

        # Locate the packed EP-present bit by comparing otherwise identical
        # canonical records. The move is deliberately unrelated to EP, so each
        # corruption below reaches the intended structural rejection path.
        packed_ep_plain.write_text(
            """fen 4k3/8/8/3pP3/8/8/8/4K3 w - d6 0 1
move e1e2
score 0
ply 1
result 0
e
""",
            encoding="utf-8",
            newline="\n",
        )
        packed_no_ep_plain.write_text(
            packed_ep_plain.read_text(encoding="utf-8").replace(
                " w - d6 ", " w - - "
            ),
            encoding="utf-8",
            newline="\n",
        )
        packed_no_ep_black_pawn_plain.write_text(
            packed_no_ep_plain.read_text(encoding="utf-8").replace(
                "3pP3", "3pp3"
            ),
            encoding="utf-8",
            newline="\n",
        )
        run_engine(
            engine,
            "convert_bin targetfile {} output_file_name {} check_illegal_move 1".format(
                packed_ep_plain, packed_ep_binary
            ),
        )
        run_engine(
            engine,
            "convert_bin targetfile {} output_file_name {} check_illegal_move 1".format(
                packed_no_ep_plain, packed_no_ep_binary
            ),
        )
        run_engine(
            engine,
            "convert_bin targetfile {} output_file_name {} check_illegal_move 1".format(
                packed_no_ep_black_pawn_plain, packed_no_ep_black_pawn_binary
            ),
        )
        run_engine(engine, "validate_training_data {}".format(packed_ep_binary))
        run_engine(engine, "validate_training_data {}".format(packed_no_ep_binary))
        packed_ep_data = packed_ep_binary.read_bytes()
        packed_no_ep_data = packed_no_ep_binary.read_bytes()
        ep_flag_bit = discover_ep_flag(packed_ep_data, packed_no_ep_data, "d6")

        inconsistent_ep_data = bytearray(packed_ep_data)
        write_lsb_bits(
            inconsistent_ep_data, ep_flag_bit + 1, 7, square_index("f6")
        )
        inconsistent_packed_ep_binary.write_bytes(inconsistent_ep_data)
        expect_validation_failure(
            engine,
            inconsistent_packed_ep_binary,
            "no possible initial-move provenance",
        )

        packed_no_ep_black_pawn_data = packed_no_ep_black_pawn_binary.read_bytes()
        pawn_color_bits = [
            bit
            for bit in range(ep_flag_bit)
            if packed_bit(packed_no_ep_data, bit)
            != packed_bit(packed_no_ep_black_pawn_data, bit)
        ]
        if len(pawn_color_bits) != 1:
            raise AssertionError(
                "changing the e5 pawn color changed {} packed bits".format(
                    len(pawn_color_bits)
                )
            )
        no_capturer_data = bytearray(packed_ep_data)
        pawn_color_bit = pawn_color_bits[0]
        write_lsb_bits(
            no_capturer_data,
            pawn_color_bit,
            1,
            packed_bit(packed_no_ep_black_pawn_data, pawn_color_bit),
        )
        no_capturer_packed_ep_binary.write_bytes(no_capturer_data)
        expect_validation_failure(
            engine,
            no_capturer_packed_ep_binary,
            "no eligible capturer",
        )

        illegal_move_plain.write_text(
            """fen rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1
move e2e5
score 0
ply 1
result 0
e
""",
            encoding="utf-8",
            newline="\n",
        )
        run_engine(
            engine,
            "convert_bin targetfile {} output_file_name {} check_illegal_move 0".format(
                illegal_move_plain, unchecked_illegal_binary
            ),
        )
        unchecked_data = unchecked_illegal_binary.read_bytes()
        if len(unchecked_data) != RECORD_SIZE:
            raise AssertionError("unchecked representable move was filtered")
        if record_field(unchecked_data, 0, MOVE_OFFSET, "<H") != 0x0324:
            raise AssertionError("unchecked e2e5 move did not use the canonical wire")
        expect_validation_failure(
            engine, unchecked_illegal_binary, "move is not legal"
        )

        run_engine(
            engine,
            "convert_bin targetfile {} output_file_name {} check_illegal_move 1".format(
                illegal_move_plain, checked_illegal_binary
            ),
            expect_success=False,
            failure_text="produced no valid records",
        )
        if checked_illegal_binary.exists():
            raise AssertionError("checked illegal move left an output file")

        stats_command = "gather_statistics position_count input_file {} output_file {}".format(
            binary, stats_output
        )
        run_engine(engine, stats_command)
        expect_refusal_without_change(engine, stats_command, stats_output)

        empty_pgn.write_text("", encoding="utf-8")
        convert_pgn = "convert_bin_from_pgn_extract targetfile {} output_file_name {}".format(
            empty_pgn, pgn_binary
        )
        run_engine(
            engine,
            convert_pgn,
            expect_success=False,
            failure_text="produced no valid records",
        )
        if pgn_binary.exists():
            raise AssertionError("empty PGN conversion left an empty dataset")

        expect_pv_generator_absent(engine, removed_pv_output)

        invalid_nonpv_result = run_engine(
            engine,
            (
                "generate_training_data_nonpv count 1 exploration_max_ply 0 "
                "output_file {} data_format bin seed tools-wire-test"
            ).format(invalid_nonpv_output),
            expect_success=False,
            timeout=5,
            failure_text="Invalid generate_training_data_nonpv parameter range",
        )
        if invalid_nonpv_output.exists():
            raise AssertionError("zero-ply non-PV generation created an output file")
        if "INFO: Executing generate_training_data_nonpv command" in invalid_nonpv_result:
            raise AssertionError("zero-ply non-PV generation reached generator setup")

        invalid_nonpv_rate_result = run_engine(
            engine,
            (
                "generate_training_data_nonpv count 1 exploration_save_rate 0 "
                "output_file {} data_format bin seed tools-wire-test"
            ).format(invalid_nonpv_rate_output),
            expect_success=False,
            timeout=5,
            failure_text="Invalid generate_training_data_nonpv parameter range",
        )
        if invalid_nonpv_rate_output.exists():
            raise AssertionError("zero-rate non-PV generation created an output file")
        if "INFO: Executing generate_training_data_nonpv command" in invalid_nonpv_rate_result:
            raise AssertionError("zero-rate non-PV generation reached generator setup")

        # The legacy position wire stores castling rights but not Atomic960 rook
        # origins. Exercise every public legacy-v1 entrypoint with Chess960
        # explicitly enabled on the fixed Atomic variant.
        legacy_inputs_before = {
            plain: plain.read_bytes(),
            exported_plain: exported_plain.read_bytes(),
            binary: binary.read_bytes(),
            empty_pgn: empty_pgn.read_bytes(),
        }
        chess960_configurations = {
            "option": ["setoption name UCI_Chess960 value true"],
        }
        for configuration_name, extra_commands in chess960_configurations.items():
            rejected_outputs = {
                name: root / "chess960-{}-{}{}".format(configuration_name, name, suffix)
                for name, suffix in [
                    ("convert-bin", ".bin"),
                    ("convert-pgn", ".bin"),
                    ("convert-plain", ".plain"),
                    ("convert-epd", ".epd"),
                    ("generate-nonpv", ".bin"),
                    ("puzzles", ".epd"),
                    ("stats", ".txt"),
                    ("nudged", ".bin"),
                    ("rescore", ".bin"),
                ]
            }
            rejected_commands = [
                (
                    "convert-bin",
                    "convert_bin targetfile {} output_file_name {}".format(
                        plain, rejected_outputs["convert-bin"]
                    ),
                ),
                (
                    "convert-pgn",
                    "convert_bin_from_pgn_extract targetfile {} output_file_name {}".format(
                        empty_pgn, rejected_outputs["convert-pgn"]
                    ),
                ),
                (
                    "convert-plain",
                    "convert_plain targetfile {} output_file_name {}".format(
                        binary, rejected_outputs["convert-plain"]
                    ),
                ),
                (
                    "convert-epd",
                    "convert_epd targetfile {} output_file_name {}".format(
                        binary, rejected_outputs["convert-epd"]
                    ),
                ),
                ("validate-plain", "validate_training_data {}".format(exported_plain)),
                ("validate-bin", "validate_training_data {}".format(binary)),
                (
                    "generate-nonpv",
                    "generate_training_data_nonpv count 1 output_file {} "
                    "data_format bin seed tools-wire-test".format(
                        rejected_outputs["generate-nonpv"]
                    ),
                ),
                (
                    "puzzles",
                    "generate_puzzles count 1 output_file_name {}".format(
                        rejected_outputs["puzzles"]
                    ),
                ),
                (
                    "stats",
                    "gather_statistics position_count input_file {} output_file {}".format(
                        binary, rejected_outputs["stats"]
                    ),
                ),
                (
                    "nudged",
                    "transform nudged_static input_file {} output_file {}".format(
                        binary, rejected_outputs["nudged"]
                    ),
                ),
                (
                    "rescore",
                    "transform rescore input_file {} output_file {}".format(
                        binary, rejected_outputs["rescore"]
                    ),
                ),
            ]
            for command_name, command in rejected_commands:
                run_engine(
                    engine,
                    command,
                    expect_success=False,
                    failure_text="cannot represent Chess960 castling state",
                    extra_commands=extra_commands,
                )
                output = rejected_outputs.get(command_name)
                if output is not None and output.exists():
                    raise AssertionError(
                        "{} Chess960 configuration let {} create {}".format(
                            configuration_name, command_name, output
                        )
                    )

        for input_path, before in legacy_inputs_before.items():
            if input_path.read_bytes() != before:
                raise AssertionError(
                    "rejected Chess960 commands modified input {}".format(input_path)
                )

        invalid_scale_output = root / "invalid-scale.bin"
        run_engine(
            engine,
            (
                "convert_bin targetfile {} output_file_name {} "
                "src_score_min_value 1 src_score_max_value 1"
            ).format(plain, invalid_scale_output),
            expect_success=False,
            failure_text="must differ",
        )
        if invalid_scale_output.exists():
            raise AssertionError("invalid conversion scale created an output file")

        special_moves_hash = hashlib.sha256(data).hexdigest().upper()
        if special_moves_hash != SPECIAL_MOVES_SHA256:
            raise AssertionError("special-move fixture hash changed: {}".format(special_moves_hash))
        print("special_moves_sha256={}".format(special_moves_hash))

    print("tools integration tests passed")


if __name__ == "__main__":
    main()
