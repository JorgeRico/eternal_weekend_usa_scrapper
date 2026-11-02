#!/usr/bin/env python3
"""Count how often each decklist appears in a Melee.gg results CSV.

The scraper writes a ``decklists`` column that can contain multiple decklist
names separated by ``; ``. By default this script splits those values, so each
individual decklist name is counted once per player.

Usage:
    python3 decklist_counts.py eternal_weekend_legacy.csv
    python3 decklist_counts.py eternal_weekend_legacy.csv -o decklists.csv
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path


def count_decklists(
    input_csv: str | Path,
    output_csv: str | Path,
    column: str = "decklists",
    split_multi: bool = True,
    include_empty: bool = False,
) -> list[tuple[str, int]]:
    """Group a results CSV by decklist name and write the counts to another CSV.

    Args:
        input_csv: Results CSV produced by ``scraper.py``.
        output_csv: Destination CSV with ``decklist,count`` rows.
        column: Name of the decklist column in the input CSV.
        split_multi: Split values on ``;`` so multi-decklist entries count each
            decklist separately.
        include_empty: Count blank decklist values as an empty-name group.

    Returns:
        The ``(decklist, count)`` pairs, sorted by count descending and then by
        name, exactly as written to ``output_csv``.
    """
    input_path = Path(input_csv)
    output_path = Path(output_csv)

    with input_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"{input_path} has no header row")
        if column not in reader.fieldnames:
            raise ValueError(
                f"Column {column!r} not found in {input_path}. "
                f"Available columns: {', '.join(reader.fieldnames)}"
            )

        counts: Counter[str] = Counter()
        for row in reader:
            raw_value = (row.get(column) or "").strip()
            if not raw_value:
                if include_empty:
                    counts[""] += 1
                continue

            names = (
                [part.strip() for part in raw_value.split(";")]
                if split_multi
                else [raw_value]
            )
            for name in names:
                if name or include_empty:
                    counts[name] += 1

    results = sorted(counts.items(), key=lambda item: (-item[1], item[0].lower()))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["decklist", "count"])
        writer.writerows(results)

    return results


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Count repeated decklist names in a Melee.gg results CSV."
    )
    parser.add_argument("input_csv", help="Results CSV (e.g. players.csv)")
    parser.add_argument(
        "-o",
        "--output",
        default="decklist_counts.csv",
        help="Destination CSV (default: decklist_counts.csv)",
    )
    parser.add_argument(
        "--column",
        default="decklists",
        help="Decklist column name (default: decklists)",
    )
    parser.add_argument(
        "--no-split",
        action="store_true",
        help="Treat multi-decklist values as a single group instead of splitting on ';'",
    )
    parser.add_argument(
        "--keep-empty",
        action="store_true",
        help="Include blank decklist values as an empty-name group",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        results = count_decklists(
            args.input_csv,
            args.output,
            column=args.column,
            split_multi=not args.no_split,
            include_empty=args.keep_empty,
        )
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Wrote {len(results)} decklists to {args.output}")
    for name, count in results[:10]:
        print(f"  {count:>5}  {name or '(empty)'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
