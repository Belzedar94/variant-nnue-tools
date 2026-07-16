# AtomicNNUEV3 symbolic reachability oracle

## Purpose and trust boundary

`script/atomic_v3_reachability_oracle.py` is the independent physical-mask
oracle required by the Atomic-Stockfish H9.3l-a publication contract. It is a
stdlib-only Python program. The mask-producing `generate` command accepts one
authenticated feature schema and two output paths; there is deliberately no
engine, trainer, shard, counter, label, manifest or dataset input.

The frozen specification is vendored byte-for-byte from Atomic-Stockfish merge
`dde43fc08fb2bd45eec09d3be9f6d06845eeb24`:

- `spec/atomic-nnue-v3.json`, 51,407 bytes, SHA-256
  `9d3c77a58e5e55ac1bc798dab41977451eb523fce1d6fd3ec3f7c1e574a78750`;
- `spec/atomic-v3-reachability-attestation-v1.json`, 11,572 bytes, SHA-256
  `fb1af7130a2fa74be0fadd721db980269e12c89204b627eec63e6074ed3983e8`.

The second `attest` command is only an evidence composer. A controlling release
job must explicitly supply descriptors whose campaign, producer, coverage
policy and oracle-binary authentication has already passed, plus the expected
SHA-256 of that exact canonical controller descriptor as an external trust
anchor. The command checks
their frozen schema identities, exact file/hash bindings and policy agreement,
then emits the canonical H9.3l-a document and evidence hash. It does not replay
the campaign or independently authenticate the producer. Missing, false or
mismatched controller claims fail closed.

## Structural-reachability domain

A physical bit is one when its individual relation has a symbolic witness in
an evaluable standard 8x8 Atomic snapshot under the frozen feature semantics:

- exactly one king of each color, at most 16 pieces per color and 32 total;
- no pawn on rank 1 or rank 8;
- one shared joint orientation per accumulator perspective, with the oriented
  perspective king on files e-h;
- CapturePair, KingBlastEP and BlastRing remain occupancy-based pseudorelations:
  pins, check evasion and self-blast are intentionally not legality filters;
- a king-absent explosion terminal is outside the NNUE domain.

This is an existential per-row structural proof, not a claim that all one-bits
can coexist in one position. It also does not inspect observed counters; an
unseen dataset row cannot manufacture structural reachability.

The four derivations are independent of the engine implementation:

1. HM enumerates the frozen 32 bucket by 11 plane by 64 square rectangle. A
   non-king row on the exact perspective-king square is impossible. Pawn rows
   on ranks 1/8 are impossible. The merged king row at the perspective-king
   square remains reachable through that king.
2. CapturePair rebuilds pawn, knight, bishop, rook and queen empty-board edge
   tables from square geometry in the specified lexicographic order. A pawn
   victim on ranks 1/8 is impossible. For actor `OPP`, target `KING` is the
   perspective king, so its oriented target file must be e-h. All 28 validated
   geometric en-passant tail rows have witnesses.
3. KingBlastEP rejects off-board directions. Whenever a directional class is
   the perspective king its related square must be on files e-h. Direct
   `OPP`-actor king targets obey the same constraint. In oriented coordinates,
   OWN en-passant centers are on rank 6 and OPP centers on rank 3.
4. BlastRing rejects off-board collateral directions. N/B/R/Q collateral is
   possible on every on-board neighbor; the surviving-pawn class is impossible
   when that neighbor lies on rank 1/8. Origin and en-passant exclusions do not
   remove a physical row when a distinct normal-capture witness exists.

Historical Discord evidence was checked through the local `fairy-vault`. In
message `970601585946288188` (2022-05-02), ubdip notes that NNUE assumes strict
positions reachable from the starting setup, motivating the physical pawn,
king and material invariants. Message `1068675519547191298` (2023-01-27) and
the already-reviewed H9.3e evidence distinguish pawn collateral survival from
king immunity. These sources inform the specification choice; no Discord or
engine implementation code is imported by the oracle.

## Frozen output

Bitmap bit `i` is stored at bit `i mod 8`, least-significant bit first. Physical
masks are concatenated in this exact order:

| Perspective | Slice | Offset | Bytes | Reachable bits |
| --- | --- | ---: | ---: | ---: |
| WHITE | HalfKAv2Atomic_hm | 0 | 2,816 | 21,200 |
| WHITE | AtomicCapturePair | 2,816 | 5,002 | 36,870 |
| WHITE | AtomicKingBlastEP | 7,818 | 288 | 1,372 |
| WHITE | AtomicBlastRing | 8,106 | 1,280 | 8,112 |
| BLACK | HalfKAv2Atomic_hm | 9,386 | 2,816 | 21,200 |
| BLACK | AtomicCapturePair | 12,202 | 5,002 | 36,870 |
| BLACK | AtomicKingBlastEP | 17,204 | 288 | 1,372 |
| BLACK | AtomicBlastRing | 17,492 | 1,280 | 8,112 |

The two normalized physical byte sets are identical, while their authenticated
per-mask hashes differ because the perspective ID is part of the hash domain.
The complete wire SHA-256 is
`1e6454345e54784936db8220b93bc87c4d2903bc8ce2b630f8cfdcbf8d484551`.
The validator derives HM training and virtual-factor masks from physical HM;
they are never additional trusted oracle output. The aggregate of all twelve
domain-separated mask hashes is
`6ff244b3c2c4e590c367205750099d34cc2cb7be47647b2dc593474c03067a0b`.

## CLI and transactions

```bash
python script/atomic_v3_reachability_oracle.py capabilities

python script/atomic_v3_reachability_oracle.py generate \
  --feature-schema spec/atomic-nnue-v3.json \
  --output atomic-v3-reachability.atmask \
  --manifest atomic-v3-reachability.manifest.json
```

Inputs are bounded regular-file snapshots opened once and checked with `fstat`
before and after. UTF-8 BOMs, duplicate JSON keys, NaN/Infinity, symlinks,
reparse points and identity changes are rejected. Outputs use same-directory
temporary files, fsync and atomic no-replace hard links. Existing paths are
never overwritten; a partial two-file publication is rolled back without
deleting a competing writer's file. The canonical manifest is the commit
marker and becomes visible last.

`attest --help` lists the eight mandatory release-controller inputs. The
controller descriptor schema is intentionally small and local: it carries
already-authenticated artifact descriptors and four explicit true assertions.
`--expected-controller-sha256` must come from the authenticated controller/CAS
boundary; recomputing it from the adjacent input inside the oracle invocation
does not establish trust and is forbidden by the publication runbook.
The final Atomic-Stockfish validator remains authoritative and must recheck the
complete publication DAG with its operator trust pins.

Run the cross-platform unit, golden, mutation and transaction suite with:

```bash
make v3-reachability-oracle-tests
```

The `atomic` GitHub workflow executes this target through `make test` on Linux
GCC, Linux Clang and Windows MinGW/Python. Legacy V1 and Atomic BIN V2 code,
schemas and commands are unchanged.
