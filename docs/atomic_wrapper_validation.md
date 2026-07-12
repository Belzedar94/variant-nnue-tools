# Atomic wrapper validation

This inventory records how H7.2-B turns the `atomic` branch into a thin,
commit-pinned tooling layer. It applies only to this branch; upstream Fairy
branches and their multi-variant release surface remain independent.

## Test migration

| Existing coverage | H7.2-B disposition |
| --- | --- |
| Legacy V1 codec units for `DATA_SIZE=512` and `1024` | Ported unchanged and run by `make tools-unit`. |
| Atomic normal moves, promotion, castling and en-passant wire fixtures | Ported unchanged; the special-move SHA-256 remains frozen. |
| Validation, conversion, transform, statistics, non-PV and puzzle commands | Ported to the temporary Atomic-only backend. |
| Fairy PV self-play generator tests | Moved to the exact pinned `atomic-stockfish-data-generator`; the duplicate source and dispatch are deleted. |
| Berolina and Georgian en-passant cases | Not applicable to the public Atomic-only backend; removed from this branch's integration matrix. |
| Intrinsic `fischerandom` rejection | Replaced by explicit Atomic960 rejection through `UCI_Chess960=true`. |
| Generic backend default variant | Replaced by a positive Atomic default/one-value combo test and a negative `UCI_Variant=chess` test. |
| Historical instrumented Fairy PV generation with `Use NNUE=false/true` | Replaced by the pinned generator in its required `pure` mode under ASan+UBSan and Valgrind. |
| Generator thread instrumentation | Delegated to, and structurally verified in, the exact pinned Atomic workflow's TSan Threads=2 smoke. |

No test is silently skipped. Cases removed from the executable matrix cover
rules that the branch deliberately no longer exposes; their shared wire and
error-handling infrastructure remains covered by Atomic fixtures.

## Required gates

- Strict engine lock plus mutation tests: wrong/uppercase/unmerged SHA,
  missing remote ref, dirty checkout, worktree/index drift, URL, schema and
  gitlink mutations.
- Generated schema-header freshness.
- GCC, Clang and MinGW builds.
- Tools codec units in both historical data sizes.
- Full tools integration with frozen special-move hash.
- Pinned Legacy Atomic V1 C++ codec unit.
- Playing engine and write-only generator builds from the pinned submodule.
- Deterministic two-record wrapper fixture and all seven generator fixtures
  passed through the tools validator.
- ASan+UBSan and strict Valgrind wrapper E2E; pinned TSan Threads=2 contract.

H7.2-B changes neither the pinned playing-engine source nor Atomic search,
evaluation or move generation. The three LOS controls therefore do not apply
to this compile-time/data-tooling-only block; they remain mandatory for any
later play-affecting change.
