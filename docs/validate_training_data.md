# validate_training_data

`validate_training_data` allows validation of training data of types `.plain` and `.bin`.

As all commands in stockfish `validate_training_data` can be invoked either from command line (as `stockfish.exe validate_training_data ...`) or in the interactive prompt.

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
and numeric domains. A failure prints the record or line context and terminates
with a non-zero exit status. Success prints the validated record count.

This legacy release gate is intentionally scoped to the 512-bit 8x8 layout
used by Atomic (two NNUE kings and non-Chess960 castling). Other board schemas
must use a validator that knows their versioned position contract.
