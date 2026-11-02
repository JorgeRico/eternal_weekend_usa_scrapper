#!/usr/bin/env python3
"""Count the most played cards in maindecks and sideboards.

Reads the ``decklist_ids`` column from a Melee.gg results CSV, downloads each
decklist, and produces two leaderboards:

- ``maindeck_top100.csv``
- ``sideboard_top100.csv``

Each decklist is fetched once and cached under ``.cache/decklists`` so reruns
are fast and the scrape can resume after an interruption.

Usage:
    python3 card_counts.py eternal_weekend_legacy.csv
    python3 card_counts.py eternal_weekend_legacy.csv --limit 100 --workers 6
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://melee.gg"
DEFAULT_CACHE_DIR = Path(".cache/decklists")
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Section headers used by Melee's plain-text decklist export.
SECTION_HEADERS = {
    "maindeck",
    "sideboard",
    "commander",
    "companion",
    "maybeboard",
    "attractions",
    "stickers",
    "contraptions",
    "tokens",
}

CARD_LINE_RE = re.compile(r"^\s*(\d+)\s+(.+?)\s*$")
PRINT_LOCK = threading.Lock()


@dataclass
class Decklist:
    id: str
    maindeck: list[dict[str, Any]] = field(default_factory=list)
    sideboard: list[dict[str, Any]] = field(default_factory=list)


# --------------------------------------------------------------------- parsing
def parse_decklist_text(text: str) -> dict[str, list[dict[str, Any]]]:
    """Parse Melee's ``#decklist-text`` block into maindeck/sideboard cards.

    The text looks like::

        MainDeck
        4 Brainstorm
        1 Island

        Sideboard
        2 Flusterstorm

    Returns a dict with ``maindeck`` and ``sideboard`` lists of
    ``{"quantity": int, "name": str}`` entries.
    """
    boards: dict[str, list[dict[str, Any]]] = {"maindeck": [], "sideboard": []}
    current: str | None = None

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue

        header = line.lower().rstrip(":")
        if header in SECTION_HEADERS:
            current = "sideboard" if header == "sideboard" else "maindeck"
            # Only MainDeck and Sideboard are counted. Commander/Companion and
            # other sections are ignored rather than mixed into the maindeck.
            if header not in {"maindeck", "sideboard"}:
                current = None
            continue

        match = CARD_LINE_RE.match(line)
        if not match or current is None:
            continue

        quantity = int(match.group(1))
        name = match.group(2).strip()
        if quantity > 0 and name:
            boards[current].append({"quantity": quantity, "name": name})

    return boards


def parse_decklist_html(html: str) -> dict[str, list[dict[str, Any]]]:
    """Parse a decklist page, preferring the plain-text export.

    Falls back to the categorized HTML view if ``#decklist-text`` is missing.
    """
    soup = BeautifulSoup(html, "html.parser")
    text_block = soup.find("pre", id="decklist-text")
    if text_block and text_block.get_text(strip=True):
        return parse_decklist_text(text_block.get_text())

    boards: dict[str, list[dict[str, Any]]] = {"maindeck": [], "sideboard": []}
    ignored = ("commander", "companion", "maybeboard", "attraction", "sticker", "contraption")
    for category in soup.select("div.decklist-category"):
        title_el = category.select_one(".decklist-category-title")
        title = title_el.get_text(" ", strip=True) if title_el else ""
        lowered = title.lower()
        if lowered.startswith(ignored):
            continue
        board = "sideboard" if lowered.startswith("sideboard") else "maindeck"

        for record in category.select(".decklist-record"):
            quantity_el = record.select_one(".decklist-record-quantity")
            name_el = record.select_one(".decklist-record-name")
            if not quantity_el or not name_el:
                continue
            try:
                quantity = int(quantity_el.get_text(strip=True))
            except ValueError:
                continue
            name = name_el.get_text(" ", strip=True)
            if quantity > 0 and name:
                boards[board].append({"quantity": quantity, "name": name})

    return boards


# -------------------------------------------------------------------- fetching
def _cache_path(cache_dir: Path, decklist_id: str) -> Path:
    return cache_dir / f"{decklist_id}.html"


def fetch_decklist(
    session: requests.Session,
    decklist_id: str,
    cache_dir: Path = DEFAULT_CACHE_DIR,
    refresh: bool = False,
    delay: float = 0.0,
    retries: int = 3,
    timeout: int = 30,
) -> Decklist:
    """Download (or read from cache) and parse one decklist."""
    cache_file = _cache_path(cache_dir, decklist_id)
    if cache_file.exists() and not refresh:
        html = cache_file.read_text(encoding="utf-8")
    else:
        if delay:
            time.sleep(delay)
        url = f"{BASE_URL}/Decklist/View/{decklist_id}"
        last_error: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                response = session.get(url, timeout=timeout)
                if response.status_code == 429 or response.status_code >= 500:
                    raise RuntimeError(f"HTTP {response.status_code}")
                if response.status_code != 200:
                    raise RuntimeError(f"HTTP {response.status_code}")
                html = response.text
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                cache_file.write_text(html, encoding="utf-8")
                break
            except (requests.RequestException, RuntimeError) as exc:
                last_error = exc
                if attempt == retries:
                    raise RuntimeError(
                        f"Could not fetch decklist {decklist_id}: {last_error}"
                    ) from exc
                time.sleep(delay + 0.5 * attempt)

    boards = parse_decklist_html(html)
    return Decklist(
        id=decklist_id,
        maindeck=boards["maindeck"],
        sideboard=boards["sideboard"],
    )


def read_decklist_ids(csv_path: str | Path, column: str = "decklist_ids") -> list[str]:
    """Read unique, non-empty decklist ids from a results CSV."""
    ids: list[str] = []
    seen: set[str] = set()

    with Path(csv_path).open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"{csv_path} has no header row")
        if column not in reader.fieldnames:
            raise ValueError(
                f"Column {column!r} not found. Available: {', '.join(reader.fieldnames)}"
            )

        for row in reader:
            raw = (row.get(column) or "").strip()
            for decklist_id in (part.strip() for part in raw.split(";")):
                if decklist_id and decklist_id not in seen:
                    seen.add(decklist_id)
                    ids.append(decklist_id)

    return ids


def load_decklists(
    decklist_ids: Iterable[str],
    cache_dir: str | Path = DEFAULT_CACHE_DIR,
    workers: int = 6,
    delay: float = 0.05,
    refresh: bool = False,
    retries: int = 3,
) -> tuple[list[Decklist], list[tuple[str, str]]]:
    """Fetch and parse many decklists concurrently.

    Returns ``(decklists, failures)`` where failures are ``(id, error)`` pairs.
    """
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    ids = list(decklist_ids)
    decklists: list[Decklist] = []
    failures: list[tuple[str, str]] = []
    completed = 0

    def worker(decklist_id: str) -> Decklist:
        # requests.Session is not thread-safe, so each worker gets its own.
        session = requests.Session()
        session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        return fetch_decklist(
            session,
            decklist_id,
            cache_dir=cache_dir,
            refresh=refresh,
            delay=delay,
            retries=retries,
        )

    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {executor.submit(worker, decklist_id): decklist_id for decklist_id in ids}
        for future in as_completed(futures):
            decklist_id = futures[future]
            completed += 1
            try:
                decklists.append(future.result())
            except Exception as exc:  # noqa: BLE001 - report and continue
                failures.append((decklist_id, str(exc)))

            if completed % 25 == 0 or completed == len(ids):
                with PRINT_LOCK:
                    print(
                        f"  parsed {completed}/{len(ids)} decklists "
                        f"({len(failures)} failures)",
                        flush=True,
                    )

    decklists.sort(key=lambda decklist: decklist.id)
    return decklists, failures


# -------------------------------------------------------------------- counting
def count_cards(decklists: Iterable[Decklist], board: str = "maindeck") -> list[dict[str, Any]]:
    """Aggregate copies and deck counts for one board.

    ``copies`` is the total number of copies across all decklists.
    ``decks`` is how many decklists include at least one copy.
    Results are sorted by copies, then decks, then card name.
    """
    if board not in {"maindeck", "sideboard"}:
        raise ValueError("board must be 'maindeck' or 'sideboard'")

    copies: Counter[str] = Counter()
    deck_counts: Counter[str] = Counter()

    for decklist in decklists:
        seen_in_deck: set[str] = set()
        for card in getattr(decklist, board):
            name = card["name"]
            copies[name] += card["quantity"]
            if name not in seen_in_deck:
                seen_in_deck.add(name)
                deck_counts[name] += 1

    rows = []
    for name, total_copies in copies.items():
        decks = deck_counts[name]
        rows.append(
            {
                "card": name,
                "copies": total_copies,
                "decks": decks,
                "avg_copies": round(total_copies / decks, 2) if decks else 0.0,
            }
        )

    rows.sort(key=lambda row: (-row["copies"], -row["decks"], row["card"].lower()))
    return rows


def most_played_maindeck_cards(
    decklists: Iterable[Decklist], limit: int = 100
) -> list[dict[str, Any]]:
    """Return the ``limit`` most played maindeck cards."""
    return count_cards(decklists, "maindeck")[:limit]


def most_played_sideboard_cards(
    decklists: Iterable[Decklist], limit: int = 100
) -> list[dict[str, Any]]:
    """Return the ``limit`` most played sideboard cards."""
    return count_cards(decklists, "sideboard")[:limit]


# ----------------------------------------------------------------------- output
def write_leaderboard(path: str | Path, rows: list[dict[str, Any]]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["card", "copies", "decks", "avg_copies"])
        writer.writeheader()
        writer.writerows(rows)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Find the most played maindeck and sideboard cards."
    )
    parser.add_argument("results_csv", help="Results CSV from scraper.py")
    parser.add_argument(
        "-o",
        "--output-dir",
        default=".",
        help="Directory for maindeck_top100.csv / sideboard_top100.csv",
    )
    parser.add_argument(
        "--limit", type=int, default=100, help="Cards per leaderboard (default: 100)"
    )
    parser.add_argument(
        "--board",
        choices=["both", "maindeck", "sideboard"],
        default="both",
        help="Which leaderboard(s) to write (default: both)",
    )
    parser.add_argument(
        "--column",
        default="decklist_ids",
        help="Decklist id column (default: decklist_ids)",
    )
    parser.add_argument(
        "--cache-dir",
        default=str(DEFAULT_CACHE_DIR),
        help=f"Decklist cache directory (default: {DEFAULT_CACHE_DIR})",
    )
    parser.add_argument(
        "--workers", type=int, default=6, help="Concurrent downloads (default: 6)"
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.05,
        help="Delay before each uncached download (default: 0.05s)",
    )
    parser.add_argument(
        "--refresh", action="store_true", help="Ignore the cache and refetch decklists"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        decklist_ids = read_decklist_ids(args.results_csv, column=args.column)
    except (OSError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if not decklist_ids:
        print("No decklist ids found in the results CSV.", file=sys.stderr)
        return 1

    print(f"Found {len(decklist_ids)} unique decklists")
    decklists, failures = load_decklists(
        decklist_ids,
        cache_dir=args.cache_dir,
        workers=args.workers,
        delay=args.delay,
        refresh=args.refresh,
    )
    print(f"Parsed {len(decklists)} decklists ({len(failures)} failures)")

    output_dir = Path(args.output_dir)
    if args.board in {"both", "maindeck"}:
        rows = most_played_maindeck_cards(decklists, limit=args.limit)
        path = output_dir / f"maindeck_top{args.limit}.csv"
        write_leaderboard(path, rows)
        print(f"\nTop {len(rows)} maindeck cards -> {path}")
        for row in rows[:10]:
            print(f"  {row['copies']:>5} copies  {row['decks']:>4} decks  {row['card']}")

    if args.board in {"both", "sideboard"}:
        rows = most_played_sideboard_cards(decklists, limit=args.limit)
        path = output_dir / f"sideboard_top{args.limit}.csv"
        write_leaderboard(path, rows)
        print(f"\nTop {len(rows)} sideboard cards -> {path}")
        for row in rows[:10]:
            print(f"  {row['copies']:>5} copies  {row['decks']:>4} decks  {row['card']}")

    if failures:
        print(f"\n{len(failures)} decklists could not be parsed:", file=sys.stderr)
        for decklist_id, error in failures[:10]:
            print(f"  {decklist_id}: {error}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
