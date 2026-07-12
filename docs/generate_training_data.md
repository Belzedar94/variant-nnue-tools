# Atomic PV training-data generation

PV self-play generation is owned by the pinned Atomic-Stockfish submodule and
is no longer compiled into the temporary Fairy-based `atomic-data-tools`
backend.

Build the authoritative generator from the repository root:

```bash
make -j2 ARCH=x86-64 data-generator
```

The artifact is
`engine/Atomic-Stockfish/src/atomic-stockfish-data-generator` (or `.exe` for
`COMP=mingw`). Its exact commit and shared schema are authenticated by
`atomic-engine.lock.json` and `tests/atomic_engine_pin.py`.

## Required setup

The current Legacy Atomic V1 generator requires a compatible Atomic NNUE and
`Use NNUE=pure`. `pure` is used only for dataset targets; games and Elo tests
use `Use NNUE=true` or `false`.

```text
uci
setoption name EvalFile value atomic_run3b_e202_l05.nnue
setoption name Use NNUE value pure
setoption name Threads value 1
setoption name Hash value 512
isready
generate_training_data depth 8 count 100000 output_file_name atomic.bin data_format bin seed 20260711
quit
```

The resolved non-zero 64-bit seed is printed as `PRNG::initial_seed`. Record
that value together with the engine commit, network SHA-256, book, options and
thread count. Byte-exact replay requires the same inputs and `Threads=1`.

## Supported parameters

The Atomic generator accepts named parameters. Important controls include:

- `depth`, or `min_depth` plus `max_depth`;
- `nodes` as an optional per-search node cap;
- `count` for the exact requested record count;
- `write_min_ply` and exclusive `write_max_ply`;
- `random_move_min_ply`, `random_move_max_ply` and `random_move_count`;
- `random_move_like_apery`;
- `random_multi_pv`, `random_multi_pv_diff` and `random_multi_pv_depth`;
- `eval_limit` and `eval_diff_limit`;
- `keep_draws`;
- `filter_captures`, `filter_checks` and `filter_promotions`;
- `adjudicate_draws_by_score` and
  `adjudicate_draws_by_insufficient_material`;
- `book`, `save_every`, `random_file_name`, `output_file_name`, `data_format`
  and `seed`.

`data_format` must be `bin`. Invalid or inverted ranges, an empty write
window, unsupported positions, invalid networks and existing output paths are
hard failures. The generator never appends to or overwrites a dataset.

## Compatibility limits

Legacy Atomic V1 writes fixed 72-byte records with a 16-bit historical move
wire. It supports normal moves, promotions, en passant and orthodox-layout
castling. It cannot encode Atomic960 rook origins, missing kings, rule-50
clocks above 127, drops, gating or large-board moves.

The generator advertises `read:false, write:true` through
`atomic_data_schema`. The tools backend advertises `read:true, write:true`
because conversion can create Legacy V1 files. The trainer advertises
`read:true, write:false`.

Run the generator's full fixture suite and validate its output with this
repository's backend before training. The dedicated Atomic workflow performs
that cross-component check with a deterministic synthetic NNUE.
