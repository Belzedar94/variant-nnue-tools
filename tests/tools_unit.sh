#!/bin/sh

set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
build_dir=$(mktemp -d "${TMPDIR:-/tmp}/variant-nnue-tools-unit.XXXXXX")
trap 'rm -rf "$build_dir"' EXIT INT TERM

"${CXX:-c++}" \
    -std=c++17 \
    -Wall -Wextra -Wpedantic -Werror \
    -fno-exceptions \
    -DDATA_SIZE=512 \
    -I"$root/src" \
    "$root/tests/tools_unit.cpp" \
    -o "$build_dir/tools_unit"

"$build_dir/tools_unit"
