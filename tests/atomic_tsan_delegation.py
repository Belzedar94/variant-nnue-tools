#!/usr/bin/env python3
"""Verify the exact pinned engine keeps the delegated Threads=2 TSan gate."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "engine" / "Atomic-Stockfish"


def require_fragments(path: Path, fragments: tuple[str, ...]) -> str:
    if not path.is_file():
        raise AssertionError(f"delegated TSan contract file is missing: {path}")
    text = path.read_text(encoding="utf-8")
    missing = [fragment for fragment in fragments if fragment not in text]
    if missing:
        formatted = "\n".join(f"  - {fragment}" for fragment in missing)
        raise AssertionError(f"{path} is missing delegated TSan clauses:\n{formatted}")
    return text


def main() -> int:
    workflow = ENGINE / ".github" / "workflows" / "atomic.yml"
    generator_test = ENGINE / "tests" / "data_generator.py"

    workflow_text = require_fragments(
        workflow,
        (
            "- name: TSan",
            "sanitizer: thread",
            "mode: thread",
            "- name: Instrumented data-generator smoke (Threads=2)",
            "TSAN_OPTIONS: halt_on_error=1:log_path=tsan-generator",
            "python3 tests/data_generator.py --smoke-only",
        ),
    )
    smoke_start = workflow_text.index(
        "- name: Instrumented data-generator smoke (Threads=2)"
    )
    smoke_end = workflow_text.find("\n      - name:", smoke_start + 1)
    smoke_step = workflow_text[smoke_start : smoke_end if smoke_end >= 0 else None]
    if "if:" in smoke_step:
        raise AssertionError(
            "the delegated data-generator smoke must run for the TSan matrix entry"
        )

    require_fragments(
        generator_test,
        (
            "(*setup_commands(net, threads=2), generation_command(multi), \"quit\")",
            'if multi_lines.count("INFO: threads = 2") != 1:',
            'raise AssertionError(f"generator did not run with exactly two threads:',
        ),
    )

    print(
        "Delegated TSan contract verified: the exact pinned Atomic-Stockfish "
        "workflow builds sanitize=thread and runs its data-generator smoke; "
        "that smoke executes Threads=2 and asserts the runtime marker."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
