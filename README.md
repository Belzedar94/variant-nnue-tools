# Atomic NNUE tools

This `atomic` branch is the dataset-tooling layer for
[Atomic-Stockfish](https://github.com/Belzedar94/Atomic-Stockfish). It is not a
second playing engine and it is not the upstream multi-variant tools release.

The authoritative PV self-play generator is compiled from the pinned
`engine/Atomic-Stockfish` submodule. This repository temporarily retains the
historical Fairy-based reader/converter backend as `atomic-data-tools` while
the Legacy Atomic V1 decoder and the future `atomic-bin-v2` implementation are
moved behind the shared Atomic core.

## Repository contract

| Role | Owner | Capability |
| --- | --- | --- |
| Playing engine | `engine/Atomic-Stockfish` | UCI/XBoard Atomic engine |
| PV data generation | `engine/Atomic-Stockfish` | Legacy Atomic V1 writer |
| Dataset tools | this repository | Legacy Atomic V1 read/write conversion, validation and statistics |
| Trainer | `variant-nnue-pytorch/atomic` | Legacy Atomic V1 reader and NNUE serialization |

`atomic-engine.lock.json` is authoritative for the engine commit, canonical
repository URL, build targets and shared schema SHA-256. The locked commit must
be merged into `Atomic-Stockfish/main`; branch heads are not accepted as pins.
`tests/atomic_engine_pin.py` authenticates one coherent Git index snapshot and
rejects a missing, dirty, conflicted or wrong-commit submodule, uncommitted lock
metadata, a changed URL/gitlink and schema drift. The compiled tools handshake
is generated from the pinned `atomic-schema.json`, then checked for staleness.

## Checkout and build

Clone with the submodule:

```bash
git clone --recurse-submodules --branch atomic \
  https://github.com/Belzedar94/variant-nnue-tools.git
cd variant-nnue-tools
```

The root Makefile exposes the supported transition-layer targets:

```bash
make verify-engine-pin
make schema-header-check
make -j2 ARCH=x86-64 data-generator
make -j2 ARCH=x86-64 data-tools
make -j2 ARCH=x86-64 playing-engine
make -j2 ARCH=x86-64 test
```

Artifacts are produced in their owning source trees:

- `engine/Atomic-Stockfish/src/atomic-stockfish-data-generator`
- `engine/Atomic-Stockfish/src/atomic-stockfish`
- `src/atomic-data-tools`

Use `COMP=mingw` on Windows from an MSYS2 MinGW64 shell; the corresponding
artifacts have an `.exe` suffix.

## Generate, validate and train

Generation uses `Use NNUE=pure`; this mode is reserved for datasets and is not
a playing-strength configuration:

```text
uci
setoption name EvalFile value atomic_run3b_e202_l05.nnue
setoption name Use NNUE value pure
setoption name Threads value 1
isready
generate_training_data depth 8 count 100000 output_file_name atomic.bin data_format bin seed 20260711
quit
```

Send that command to the `atomic-stockfish-data-generator` artifact. The
temporary `atomic-data-tools` backend deliberately does not expose
`generate_training_data`; it owns `validate_training_data`, `convert_bin`,
`convert_plain`, `convert_epd`, `convert_bin_from_pgn_extract`, `convert`,
`transform` and `gather_statistics` until they are ported to the shared core.
It starts in Atomic, advertises only `UCI_Variant=atomic`, rejects attempts to
select another variant, and does not load mutable external variant definitions.

Validate every generated shard before training:

```text
uci
setoption name UCI_Variant value atomic
validate_training_data atomic.bin
quit
```

The current compatibility format is the headerless 72-byte Legacy Atomic V1
wire. It rejects append/overwrite, truncated records, invalid moves and
Atomic960. See [the frozen contract](docs/legacy_atomic_v1.md) and
[generation options](docs/generate_training_data.md).

## Tests and CI

The dedicated `Atomic tools` workflow builds the pinned engine/generator and
the temporary backend, runs positive and negative pin tests, codec/integration
tests, and passes every deterministic Atomic generator fixture through the
tools validator. It also runs the cross-component path under ASan+UBSan and
strict Valgrind, verifies the pinned Threads=2 TSan generator gate, and executes
the pinned C++ codec unit on GCC, Clang and MinGW. The upstream Fairy workflows
no longer run for PRs targeting this independent `atomic` branch. The complete
test migration inventory is in
[Atomic wrapper validation](docs/atomic_wrapper_validation.md).

This is H7.2-B of the Atomic-Stockfish migration. A later block will replace
the temporary backend with the shared Atomic decoder and introduce
`atomic-bin-v2`, including a versioned header, 32-bit move wire and Atomic960
metadata.

## License

The retained Fairy-Stockfish-derived code is distributed under GPL-3.0. See
[Copying.txt](Copying.txt). Atomic-Stockfish is pinned as source, not bundled
as a generated binary.
