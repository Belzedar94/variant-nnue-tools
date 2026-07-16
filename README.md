# Atomic NNUE tools

This `atomic` branch is the dataset-tooling layer for
[Atomic-Stockfish](https://github.com/Belzedar94/Atomic-Stockfish). It is not a
second playing engine and it is independent of the upstream multi-variant tools
release.

Two explicit data contracts coexist during the migration:

- `legacy-atomic-v1` uses the retained Fairy-derived `atomic-data-tools`
  executable for the historical headerless 72-byte format;
- `atomic-bin-v2` uses `script/atomic_bin_v2_tools.py`, a fail-closed launcher
  for the validator and lossless decoder compiled from the pinned
  Atomic-Stockfish submodule.

AtomicNNUEV3 publication adds one independent evidence tool without changing
either data format: `script/atomic_v3_reachability_oracle.py` authenticates the
frozen V3 feature schema and symbolically reproduces the eight physical
reachability masks. It imports no engine, trainer or dataset code and accepts
no dataset input. See [the V3 reachability contract](docs/atomic_v3_reachability_oracle.md).

The wrapper never guesses a format from a filename or file contents. V2 can be
opened only through its canonical `.atbin.manifest.json` sidecar. Legacy V1
remains available under explicit legacy targets and commands.

## Repository contract

| Role | Owner | Capability |
| --- | --- | --- |
| Playing engine | `engine/Atomic-Stockfish` | UCI/XBoard Atomic engine |
| Data generation | `engine/Atomic-Stockfish` | Legacy V1 and Atomic BIN V2 writers |
| Legacy dataset tools | this repository | V1 validation, conversion and statistics |
| V2 dataset tools | `engine/Atomic-Stockfish` | Manifest-authenticated validation and bounded lossless decode |
| V2 command launcher | this repository | Authenticated byte-exact delegation to the pinned data tools |
| V3 reachability oracle | this repository | Dataset-independent physical masks and H9.3l-a evidence composition |
| Trainer | `variant-nnue-pytorch/atomic` | Version-specific dataset readers and NNUE serialization |

`atomic-engine.lock.json` is authoritative for the engine commit, repository
URL, data/manifest/decode schemas, artifacts and data-tools capability
response. The locked commit
must be merged into `Atomic-Stockfish/main`; branch heads are not accepted as
pins. `make verify-engine-pin` authenticates one coherent Git index snapshot
and rejects a missing, dirty, conflicted or wrong-commit submodule, changed
URL/gitlink, stale lock data, schema drift and a mismatched V2 capability.

The V2 launcher reruns the same index/gitlink/source verifier, then executes
`capabilities` before delegating and requires the child's canonical UTF-8
response to match the lock byte for byte, including its single LF. A mismatched
source checkout or incompatible child contract fails before a dataset is
opened. Contract version 1 is the compatibility identity of the generated
artifact; the launcher does not claim a cryptographic signature over a local
compiler output.

## Checkout and build

Clone the branch with its pinned submodule:

```bash
git clone --recurse-submodules --branch atomic \
  https://github.com/Belzedar94/variant-nnue-tools.git
cd variant-nnue-tools
```

The root Makefile keeps the two toolchains visibly separate:

```bash
make verify-engine-pin
make -j2 ARCH=x86-64 legacy-data-tools
make -j2 ARCH=x86-64 v2-data-tools
make -j2 ARCH=x86-64 data-generator
make -j2 ARCH=x86-64 playing-engine
```

`make data-tools` remains a compatibility alias for
`make legacy-data-tools`; it does not select or build V2. Produced artifacts
are:

- `src/atomic-data-tools` — Legacy Atomic V1;
- `engine/Atomic-Stockfish/src/atomic-stockfish-data-tools` — Atomic BIN V2;
- `engine/Atomic-Stockfish/src/atomic-stockfish-data-generator` — both writers;
- `engine/Atomic-Stockfish/src/atomic-stockfish` — playing engine.

Windows MinGW artifacts have an `.exe` suffix. Use `COMP=mingw` from an MSYS2
MinGW64 shell.

## Generate, validate and decode

Generation uses `Use NNUE=pure`. This mode is reserved for datasets and must
not be used for Elo or OpenBench play:

```text
uci
setoption name EvalFile value atomic_run3b_e202_l05.nnue
setoption name Use NNUE value pure
setoption name Threads value 1
isready
generate_training_data depth 8 count 100000 output_file_name atomic data_format atomic-bin-v2 seed 20260711
quit
```

Send the command to `atomic-stockfish-data-generator`. For Legacy V1, select
`data_format bin`; its validator remains the UCI command:

```text
validate_training_data atomic.bin
```

For V2, pass only the completed manifest sidecar to the wrapper:

```bash
python script/atomic_bin_v2_tools.py capabilities
python script/atomic_bin_v2_tools.py validate \
  --format atomic-bin-v2 \
  --manifest atomic.atbin.manifest.json
python script/atomic_bin_v2_tools.py decode \
  --format atomic-bin-v2 \
  --manifest atomic.atbin.manifest.json \
  --offset 0 \
  --limit 16
```

Both operations require named `--format` and `--manifest` arguments;
`decode` additionally requires `--limit 1..4096` and accepts an optional
unsigned `--offset` (default `0`). Options are order-independent at the child,
while the launcher relays the original argument vector unchanged, including
Unicode paths. A raw `.atbin` path, positional input, omitted `--format`, or any
unsupported format is an error. The launcher does not synthesize a manifest
path, scan sibling shards, or inspect file magic to infer V2. For the
contractual exits `0`, `2` and `3`, it preserves the child exit class and exact
stdout/stderr bytes. An unexpected process failure becomes a fail-closed
launcher error without relaying unauthenticated child output.

V2 validation and decode authenticate the canonical sidecar and every declared
shard through the pinned C++ reader. Decode buffers only the requested slice
but validates, semantically decodes and byte-re-encodes the complete dataset
before emitting its versioned UTF-8/LF JSONL. Success covers exact size and
SHA-256, header/schema/count agreement, canonical records, Atomic legal moves,
Atomic960 metadata and aggregate statistics. See
[Atomic BIN V2](docs/atomic_bin_v2.md) and
[the frozen Legacy V1 contract](docs/legacy_atomic_v1.md).

## AtomicNNUEV3 reachability evidence

Generate the immutable 18,772-byte WHITE-then-BLACK physical-mask wire and its
canonical manifest:

```bash
python script/atomic_v3_reachability_oracle.py generate \
  --feature-schema spec/atomic-nnue-v3.json \
  --output atomic-v3-reachability.atmask \
  --manifest atomic-v3-reachability.manifest.json
```

The command never overwrites, rejects symbolic links, detects input-identity
changes and destination races, and makes the manifest visible only after the
binary is ready. The separate `attest`
command requires controller-authenticated campaign, producer, coverage-policy
and oracle-binary descriptors plus their exact externally supplied controller
SHA-256 trust anchor. Their absence or mismatch is fatal; the oracle does not
turn self-declared producer metadata into authenticated evidence.

## Tests and CI

Focused targets are:

```bash
make -j2 ARCH=x86-64 test
make -j2 ARCH=x86-64 v2-data-tools-tests
make -j2 ARCH=x86-64 v2-tools-unit
make v3-reachability-oracle-tests
make -j2 ARCH=x86-64 ATOMIC_NNUE_TEST_NET=/path/to/atomic.nnue v2-tools-integration
```

`v2-tools-integration` generates a real V2 fixture with the pinned generator,
then compares direct-child and wrapper behavior for capabilities, validation,
lossless decode, CLI errors, late corruption and raw-shard rejection. CI runs
the legacy and V2 contracts on GCC,
Clang and MinGW, plus ASan+UBSan and strict Valgrind lanes. The migration
inventory and exact gates are in
[Atomic wrapper validation](docs/atomic_wrapper_validation.md).

H7.5 introduces only data-tool pinning, delegation, validation and decode
behavior.
The submodule advances to an already merged and independently gated
Atomic-Stockfish commit; this wrapper block adds no search, evaluation, time or
move-generation change. No Elo/LOS test applies to H7.5. Play-affecting engine
changes retain the project's normal OpenBench gates.

## License

The retained Fairy-Stockfish-derived code is distributed under GPL-3.0. See
[Copying.txt](Copying.txt). Atomic-Stockfish is pinned as source, not bundled as
a generated binary.
