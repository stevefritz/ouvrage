#!/usr/bin/env python3
"""
Deterministic coverage analysis script.

Opens the .coverage SQLite database, extracts per-test line coverage,
and identifies tests whose coverage is fully redundant (every line they
cover is also covered by other remaining tests).

Uses a greedy set-cover approach:
1. Sort tests by coverage size descending (alphabetically for ties)
2. Walk through tests in order, accumulating covered lines
3. If a test adds zero new lines, mark it redundant

Output: JSON manifest with redundant/kept lists and stats.
"""

import json
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

# coverage.numbits is the canonical decoder for the line_bits bitmap format
from coverage.numbits import numbits_to_nums


def load_coverage_data(db_path: str) -> dict:
    """Load per-test coverage from .coverage SQLite DB.

    Returns dict mapping test_context -> set of (file_path, line_no) tuples.
    """
    conn = sqlite3.connect(db_path)

    # Load file paths
    files = {}
    for row in conn.execute("SELECT id, path FROM file"):
        files[row[0]] = row[1]

    # Load contexts
    contexts = {}
    for row in conn.execute("SELECT id, context FROM context"):
        contexts[row[0]] = row[1]

    # Load line_bits and decode
    test_coverage = defaultdict(set)
    for row in conn.execute("SELECT file_id, context_id, numbits FROM line_bits"):
        file_id, ctx_id, numbits = row
        file_path = files.get(file_id, "")
        context = contexts.get(ctx_id, "")

        # Skip empty context (module-level code) and non-test contexts
        if not context or not context.startswith("tests."):
            continue

        # Only count production lines (switchboard/ package)
        if "switchboard/" not in file_path:
            continue

        lines = numbits_to_nums(numbits)
        for line in lines:
            test_coverage[context].add((file_path, line))

    conn.close()
    return dict(test_coverage)


def greedy_set_cover(test_coverage: dict) -> tuple:
    """Greedy set-cover to identify redundant tests.

    Sort tests by coverage size descending, then alphabetically for
    determinism. Walk through in order, accumulating covered lines.
    Tests that add zero new lines are redundant.

    Returns (kept_tests, redundant_tests) as lists of dicts.
    """
    # Sort: largest coverage first, then alphabetically for determinism
    sorted_tests = sorted(
        test_coverage.items(),
        key=lambda x: (-len(x[1]), x[0])
    )

    covered_so_far = set()
    kept = []
    redundant = []

    for test_id, lines in sorted_tests:
        new_lines = lines - covered_so_far
        if new_lines:
            # This test contributes unique coverage — keep it
            covered_so_far |= new_lines
            kept.append({
                "test_id": test_id,
                "file": _test_file_from_context(test_id),
                "covered_lines": len(lines),
                "unique_lines": len(new_lines),
            })
        else:
            # All lines covered by already-kept tests — redundant
            redundant.append({
                "test_id": test_id,
                "file": _test_file_from_context(test_id),
                "covered_lines": len(lines),
                "reason": "zero unique line contribution",
            })

    return kept, redundant


def _test_file_from_context(context: str) -> str:
    """Convert test context like 'tests.test_foo.TestBar.test_baz' to 'tests/test_foo.py'."""
    parts = context.split(".")
    if len(parts) >= 2:
        return f"{parts[0]}/{parts[1]}.py"
    return context


def main():
    db_path = sys.argv[1] if len(sys.argv) > 1 else ".coverage"
    output_path = sys.argv[2] if len(sys.argv) > 2 else "coverage_manifest.json"

    print(f"Loading coverage data from {db_path}...")
    test_coverage = load_coverage_data(db_path)
    print(f"  Found {len(test_coverage)} tests with production line coverage")

    # Compute total unique lines
    all_lines = set()
    for lines in test_coverage.values():
        all_lines |= lines
    print(f"  Total unique production lines covered: {len(all_lines)}")

    print("Running greedy set-cover analysis...")
    kept, redundant = greedy_set_cover(test_coverage)

    # Verify: kept tests cover all lines
    kept_ids = {k["test_id"] for k in kept}
    kept_lines = set()
    for test_id, lines in test_coverage.items():
        if test_id in kept_ids:
            kept_lines |= lines
    assert kept_lines == all_lines, "BUG: kept tests don't cover all lines!"

    # Aggregate by file for redundant tests
    redundant_by_file = defaultdict(list)
    for r in redundant:
        redundant_by_file[r["file"]].append(r["test_id"])

    manifest = {
        "stats": {
            "before": len(test_coverage),
            "after": len(kept),
            "redundant_count": len(redundant),
            "lines_covered": len(all_lines),
            "reduction_pct": round(len(redundant) / len(test_coverage) * 100, 1),
        },
        "redundant_by_file": {
            f: sorted(tests) for f, tests in sorted(redundant_by_file.items())
        },
        "redundant": sorted(redundant, key=lambda x: x["test_id"]),
        "kept": sorted(kept, key=lambda x: x["test_id"]),
    }

    with open(output_path, "w") as f:
        json.dump(manifest, f, indent=2)

    print(f"\nResults:")
    print(f"  Tests before: {manifest['stats']['before']}")
    print(f"  Tests after:  {manifest['stats']['after']}")
    print(f"  Redundant:    {manifest['stats']['redundant_count']} ({manifest['stats']['reduction_pct']}%)")
    print(f"  Lines covered: {manifest['stats']['lines_covered']}")
    print(f"\nManifest written to {output_path}")

    # Summary of files with all tests redundant
    all_tests_by_file = defaultdict(list)
    for test_id in test_coverage:
        all_tests_by_file[_test_file_from_context(test_id)].append(test_id)

    fully_redundant_files = []
    for f, tests in sorted(redundant_by_file.items()):
        total_in_file = len(all_tests_by_file.get(f, []))
        if len(tests) == total_in_file and total_in_file > 0:
            fully_redundant_files.append(f)

    if fully_redundant_files:
        print(f"\nFiles with ALL tests redundant ({len(fully_redundant_files)}):")
        for f in fully_redundant_files:
            print(f"  {f} ({len(redundant_by_file[f])} tests)")

    return manifest


if __name__ == "__main__":
    main()
