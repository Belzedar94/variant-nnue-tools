# validate_training_data

This command is provided by the temporary `atomic-data-tools` backend. Atomic
PV generation is owned by the pinned Atomic-Stockfish submodule.

`validate_training_data` allows validation of training data of types `.plain` and `.bin`.

It can be invoked from the command line as
`atomic-data-tools validate_training_data ...` or in the interactive prompt.

The syntax of this command is as follows:
```
validate_training_data in_path
```

`in_path` is the path to the file to validate. The type of the data is deduced based on its extension (one of `.plain`, `.bin`).

Set `UCI_Variant` to the dataset variant before invoking the command. A valid
`.bin` must use the historical 72-byte v1 layout and pass all of these checks:

- complete record boundary and zero padding;
- at least one record;
- result in `-1`, `0`, or `1`;
- safely decodable and canonical 512-bit position for the selected 8x8 variant;
- canonical historical 16-bit move encoding; and
- a move legal in the decoded position.

Plain validation checks record framing, FEN/move presence, representability,
and numeric domains. A consistent standard-FEN en-passant target is accepted
when Fairy's X-FEN serializer normalizes it to `-` because no opposing pawn can
capture; inconsistent targets still fail. A failure prints the record or line
context and terminates with a non-zero exit status. Success prints the validated
record count.

This legacy release gate is intentionally scoped to the 512-bit 8x8 layout
used by Atomic (two NNUE kings and non-Chess960 castling). Other board schemas
must use a validator that knows their versioned position contract.
