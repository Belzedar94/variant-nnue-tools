# Legacy Atomic v1 pipeline

This compatibility line is based on `variant-nnue-tools` commit
`c8df2c39515a2654d5b52ba55b4ee585b20430a8`. It exists to keep the current
Atomic HalfKAv2 training pipeline reproducible while the versioned successor
format is designed. The base commit and all local changes must be recorded in
the dataset manifest; a branch name alone is not a source pin.

## Engine setup

Set the engine options before invoking a tool command:

```text
uci
setoption name UCI_Variant value atomic
setoption name Threads value 1
setoption name Use NNUE value pure
setoption name EvalFile value atomic_run3b_e202_l05.nnue
isready
generate_training_data ... seed <fixed-seed> data_format bin
```

`Use NNUE=pure` is a data-generation mode. Playing-strength tests use
`Use NNUE=true`; `pure` is not a fourth playing configuration. This legacy
Fairy loader still uses its variant-compatible filename/load checks, so the
exact loaded network must also be recorded by SHA-256 in the dataset manifest.

A textual or numeric `seed` is resolved once to a non-zero 64-bit number and
printed as `PRNG::initial_seed`. Reuse that printed decimal value to replay the
run. Byte-for-byte reproduction additionally requires the same binary, network,
opening book, options, and `Threads=1`. Per-thread random streams are stable,
but multi-thread scheduling intentionally does not promise identical record
ordering.

## Historical wire contract

The `.bin` output is the historical, headerless `PackedSfenValue` layout:

| Offset | Bytes | Field |
| ---: | ---: | --- |
| 0 | 64 | packed 512-bit position |
| 64 | 2 | signed score |
| 66 | 2 | historical move wire |
| 68 | 2 | ply |
| 70 | 1 | result relative to side to move (`-1`, `0`, `1`) |
| 71 | 1 | zero padding |

The format is 72 bytes per record on the supported little-endian builds. The
move is explicitly translated to the old 16-bit Stockfish encoding; casting a
modern 32-bit Fairy move is forbidden. Normal moves, knight-through-queen
promotions, en passant, and castling are representable. Drops, gating, fairy
promotions, large-board squares, null moves, and other modern move types fail
explicitly instead of being truncated.

The format has no magic, version, byte order marker, schema hash, or record
count. Therefore it must not be confused with the future `atomic-bin-v2`.

The packed position also predates Chess960 rook-origin metadata. Legacy v1
commands reject `UCI_Chess960=true` instead of silently emitting ambiguous
castling state. Atomic960 dataset support therefore belongs in the versioned
successor format; this does not limit Atomic960 support in the playing engine.

## File and validation policy

Generation, conversion, EPD export, and statistics output reserve a new path
with exclusive-create semantics. They never append to or overwrite an existing
path. Remove or rename an old artifact explicitly before rerunning a command.
Empty datasets are hard errors; converters remove a newly reserved output when
no valid record was produced.

Validate every shard before training:

```text
validate_training_data shard.bin
```

For `.bin`, validation checks the 72-byte boundary, deterministic padding,
result domain, safe/canonical packed position for the selected variant,
historical move encoding, and move legality. For `.plain`, it checks record
framing and the FEN/move/numeric fields. Validation errors terminate with a
non-zero exit status so CI and dataset jobs cannot silently continue.

The legacy format is retained for compatibility, not extended. New fields or
move types require a new versioned format.

The local end-to-end gate can also verify that `pure` survives Atomic network
selection:

```text
python tests/tools_integration.py --engine src/stockfish --nnue atomic_run3b_e202_l05.nnue
```
