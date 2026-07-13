# Atomic wrapper validation

This inventory records how H7.3-C3 keeps `variant-nnue-tools/atomic` a thin,
commit-pinned data layer. It applies only to this branch; upstream Fairy
branches and their multi-variant release surface remain independent.

## Ownership and test migration

| Coverage | Disposition |
| --- | --- |
| Legacy V1 codec units for `DATA_SIZE=512` and `1024` | Retained unchanged under `legacy-tools-unit`; `tools-unit` remains an alias. |
| Legacy validation, conversion, transforms and statistics | Retained in `src/atomic-data-tools`, built explicitly by `legacy-data-tools`; `data-tools` remains a compatibility alias. |
| Legacy normal, promotion, castling and en-passant fixtures | Retained with the frozen special-move SHA-256. |
| PV self-play generation | Owned only by the exact pinned `atomic-stockfish-data-generator`. |
| Atomic BIN V2 header, record, sink, manifest and reader units | Owned and run by the pinned Atomic-Stockfish targets. |
| Production V2 CLI black-box contract | Delegated to the pinned engine's `data-tools-tests` target. |
| Wrapper V2 command surface | Covered by `tests/test_atomic_bin_v2_tools.py`, including capability authentication and exact byte/exit forwarding for contract exits `0`, `2` and `3`. |
| Cross-component V2 path | A real pinned generator fixture is validated both directly and through `script/atomic_bin_v2_tools.py`. |
| Raw `.atbin` input | Negative coverage at child and wrapper boundaries; no path inference is permitted. |
| Atomic960 V2 records | Positive reader/statistics coverage; Legacy V1 continues to reject Atomic960. |
| Berolina and Georgian en-passant | Not applicable to the Atomic-only public surface. |
| Historical Fairy generator instrumentation | Replaced by pinned generator and wrapper-level ASan+UBSan, Valgrind and delegated Threads=2 TSan gates. |

No test is silently skipped. Cases removed from the executable matrix cover
variants that this branch deliberately does not expose; shared framing, wire
and error handling stay covered by Atomic fixtures.

## Build boundary

Every engine-owned target depends on `verify-engine-pin` at the root:

```text
v2-data-tools       -> engine/Atomic-Stockfish/src data-tools
v2-data-tools-tests -> engine/Atomic-Stockfish/src data-tools-tests
data-generator      -> engine/Atomic-Stockfish/src data-generator
```

The wrapper does not recompile or relink the C++ reader. The production V2
artifact remains `engine/Atomic-Stockfish/src/atomic-stockfish-data-tools`
(`.exe` on MinGW). The Python launcher resolves only that locked artifact,
reruns the index/gitlink/source verifier, executes `capabilities`, and compares
its canonical response byte-for-byte with `data_tools_contract.capabilities`
in `atomic-engine.lock.json` before delegating. The lock and contract version
authenticate source provenance and semantic compatibility; they are not a
cryptographic signature of a machine-local compiler output.

## Required gates

- Strict engine lock and mutation tests: SHA/ref, dirty/conflicted checkout,
  index/worktree drift, URL/gitlink, both V2 schemas, build target/artifact and
  exact capabilities mutations.
- Generated Legacy schema-header freshness.
- GCC, Clang and MinGW builds for Legacy V1, the V2 child and the launcher path.
- Legacy codec units in both historical data sizes and full tools integration.
- Pinned Legacy V1 C++ codec unit.
- Pinned V2 production CLI suite, including canonical response bytes,
  authenticated multi-shard streaming, Atomic960 statistics and indexed
  corruption diagnostics.
- Launcher unit tests for a wrong/missing child, capability mismatch, argument
  preservation, stdout/stderr bytes and contract exit codes `0`, `2` and `3`.
- Deterministic cross-component Legacy fixture plus real V2 generation and
  direct-versus-wrapper validation parity.
- ASan+UBSan execution of the C++ child and wrapper E2E.
- Strict Valgrind execution of the generated-data path and V2 validator.
- Structural delegation to the exact pinned Threads=2 TSan generator gate.

## Manifest-only invariant

The only accepted V2 operation is:

```bash
python script/atomic_bin_v2_tools.py validate \
  --format atomic-bin-v2 \
  --manifest /path/run.atbin.manifest.json
```

The wrapper forwards the two option/value pairs without converting them to
positional input and without changing the supplied path. It never:

- treats a raw `.atbin` shard as a dataset;
- adds `.manifest.json` to an input;
- searches a directory for a plausible sidecar;
- infers a format from an extension, magic or JSON contents; or
- falls back to Legacy V1 when V2 validation fails.

This same explicit manifest entrypoint is the required contract for the
trainer reader. Legacy V1 remains an independent, explicitly selected
headerless format.

## Playing-strength gate

H7.3-C3 changes the root Makefile, CI, lock verification, launcher and data
documentation. Its submodule update selects an already merged and independently
gated Atomic-Stockfish commit; C3 adds no search, evaluation, time-management or
move-generation code. This is a data-only wrapper block, so no Elo, LOS or
OpenBench match is applicable. Any future change that can affect play must use
the normal playing-strength gates.
