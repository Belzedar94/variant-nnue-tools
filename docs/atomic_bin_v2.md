# Atomic BIN V2

Atomic BIN V2 is the versioned dataset contract for Atomic and Atomic960 NNUE
training. Atomic-Stockfish owns the writer, schemas and authoritative C++
reader. This repository supplies a pinned, fail-closed command launcher; it
does not duplicate the codec.

The normative files are in the locked engine submodule:

- `schemas/atomic-bin-v2.json`, SHA-256
  `0352b036f2a140c609e3eb9c9d635dc553e8d77253d8faa92437390f5cf93cb6`;
- `schemas/atomic-bin-v2-manifest.json`, SHA-256
  `83d63922df3ac4a0c81a21ec9d9fd9e180efe50f26efee62fe01710e09da5b42`;
- `schemas/atomic-data-tools-decode-v1.json`, SHA-256
  `5e3f8d7c6db6ee955b71747ee063859e15609adb557a3754228a606f3df2caad`.

This document is an operational guide. The exact pinned JSON schemas and C++
layout assertions are authoritative.

## Wire summary

Every little-endian `.atbin` shard starts with a 96-byte header:

| Offset | Bytes | Field |
| ---: | ---: | --- |
| 0 | 8 | `ATBINV2\0` magic |
| 8 | 2 | version `2` |
| 10 | 2 | header size `96` |
| 12 | 4 | endian marker `0x01020304` |
| 16 | 4 | record size `64` |
| 20 | 4 | zero flags |
| 24 | 32 | raw SHA-256 of the exact data schema |
| 56 | 8 | nonzero record count |
| 64 | 32 | zero reserved bytes |

Each 64-byte record contains:

| Offset | Bytes | Field |
| ---: | ---: | --- |
| 0 | 48 | canonical Atomic position |
| 48 | 4 | signed score, relative to side to move |
| 52 | 4 | versioned move wire |
| 56 | 4 | ply |
| 60 | 1 | result relative to side to move (`-1`, `0`, `1`) |
| 61 | 1 | flags; bit 0 is Atomic960 |
| 62 | 2 | zero reserved bytes |

The position records all 64 squares, side to move, castling rights and rook
origins, en-passant square, rule-50 count and fullmove number. The move wire
stores six-bit origin and destination squares, a four-bit move type and a
four-bit promotion. Its upper 12 bits are zero. Castling destinations use the
rook-origin square, so Atomic960 state round-trips without relying on FEN
shorthand.

Decoding is semantic, not merely structural: each record must reconstruct one
king of each color, canonical castling/en-passant state and an Atomic-legal
move, then re-encode to the exact original 64 bytes.

## Manifest is the dataset entrypoint

A completed dataset is opened only through its adjacent canonical
`.atbin.manifest.json` sidecar. The manifest is minified UTF-8 without BOM,
uses schema declaration key order, has exactly one trailing LF, and contains no
timestamps or absolute paths. It records:

- exact engine commit/version and network basename/SHA-256;
- opening-book identity or the built-in start position;
- resolved seed, Atomic960 flag, threads, hash, `Use NNUE=pure` and every
  effective generator option;
- total records and draws as full-range decimal strings; and
- every shard's contiguous index, portable basename, record count, byte count
  and SHA-256.

The reader parses this sidecar strictly, captures absolute paths once, and then
authenticates one private shard snapshot at a time. It verifies schema, size,
hash and count before exposing that shard's records and reconciles decoded
statistics with the manifest at EOF. Network and book fields are provenance;
the completed dataset reader does not require those original inputs to remain
beside the sidecar.

A raw `.atbin` shard is not a dataset entrypoint. Tools and trainers must not
append `.manifest.json`, search siblings, inspect magic, or infer V2 from an
extension or file contents. This rule avoids selecting an incomplete,
reordered or unauthenticated shard set.

## Wrapper commands

Build and test the exact pinned validator from the repository root:

```bash
make verify-engine-pin
make -j2 ARCH=x86-64 v2-data-tools
make -j2 ARCH=x86-64 v2-data-tools-tests
```

Use the wrapper rather than discovering an arbitrary executable on `PATH`:

```bash
python script/atomic_bin_v2_tools.py capabilities
python script/atomic_bin_v2_tools.py validate \
  --format atomic-bin-v2 \
  --manifest /data/run.atbin.manifest.json
python script/atomic_bin_v2_tools.py decode \
  --format atomic-bin-v2 \
  --manifest /data/run.atbin.manifest.json \
  --offset 0 \
  --limit 16
```

`validate` requires both named arguments; their order is irrelevant. The
launcher passes the received option/value pairs and manifest path unchanged.
It first verifies the authenticated index, gitlink, merged source commit and
schema files, then runs the pinned child `capabilities` command and compares
the complete response with `atomic-engine.lock.json`. Only this exact contract
is accepted:

```json
{"type":"atomic-data-tools-capabilities","contract_version":1,"formats":{"atomic-bin-v2":{"data_schema_sha256":"0352b036f2a140c609e3eb9c9d635dc553e8d77253d8faa92437390f5cf93cb6","manifest_schema_sha256":"83d63922df3ac4a0c81a21ec9d9fd9e180efe50f26efee62fe01710e09da5b42","decode_schema_sha256":"5e3f8d7c6db6ee955b71747ee063859e15609adb557a3754228a606f3df2caad","entrypoint":"manifest","read":true,"write":false,"operations":["validate","decode"]}}}
```

`decode` requires `--limit 1..4096`; `--offset` is an unsigned decimal record
index and defaults to zero. Its output is one schema-versioned header, exactly
`limit` lossless record lines and one validation footer. The complete manifest
dataset is authenticated, semantically decoded and byte-re-encoded before any
stdout is committed, including records after the requested slice. It therefore
cannot present a valid-looking prefix from a corrupt later shard.

For the supported contract exits, the wrapper preserves the child's exit class
and exact stdout/stderr bytes. It does not reinterpret validation/decode errors
or fall back to Legacy V1. The child contract uses exit `0` for success, `2`
for a CLI/contract error and `3` for parser, authentication or semantic
failure. Any other process exit becomes a fail-closed launcher error `3`
without relaying the child output.
The source pin plus contract version establish provenance and semantic
compatibility; the wrapper does not claim a cryptographic signature over the
platform-specific compiler output in the ignored build tree.

## Generation and training boundary

The pinned `atomic-stockfish-data-generator` writes V2 only when the format is
selected explicitly:

```text
setoption name EvalFile value /absolute/path/to/atomic.nnue
setoption name Use NNUE value pure
setoption name Threads value 1
isready
generate_training_data depth 3 count 1000000 output_file_name run data_format atomic-bin-v2 seed run-001
```

Set `UCI_Chess960=true` before generation for Atomic960 records. Output shards
and the sidecar use exclusive creation: append and overwrite are forbidden.
`Use NNUE=pure` is a data-generation mode, never a playing configuration.

The trainer's V2 reader is a separate Hito 7 integration. It must accept the
same manifest-only entrypoint and validate the same locked schema before
loading records. The historical 72-byte Legacy V1 reader remains an explicit
compatibility path and is not selected by failed V2 detection.

## H7.5 wrapper gates

The root workflow runs:

- pinned lock/data/manifest/decode-schema/capability mutation tests;
- V2 C++ production contract tests on GCC, Clang and MinGW;
- Python launcher unit tests and direct-versus-wrapper validation/decode E2E,
  including Unicode paths, argument-order preservation, exact stream/exit
  relay and late-corruption atomic-output checks;
- real V2 generation followed by manifest-only validation and decode;
- ASan+UBSan and strict Valgrind execution; and
- the unchanged Legacy V1 pipeline in parallel.

H7.5 adds no playing-source, search, evaluation, time-management or
move-generation behavior. The selected engine commit was already merged and
gated in Atomic-Stockfish. This wrapper block is data-only and requires no
Elo/LOS test.
