#!/usr/bin/env python3
"""Scrape all player standings from a Melee.gg tournament.

Melee.gg renders the standings table client-side from a DataTables endpoint:

    POST https://melee.gg/Standing/GetRoundStandings

The endpoint is paginated (max 500 rows per request), so this script walks the
pages and writes a flat JSON + CSV export with one row per player.

Usage:
    python scraper.py https://melee.gg/Tournament/View/491103#standings
    python scraper.py <url> --round Finals -o eternal_weekend
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://melee.gg"
PAGE_SIZE = 500  # server-side maximum
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

CSV_FIELDS = [
    "rank",
    "player_id",
    "team_id",
    "display_name",
    "name_last_first",
    "username",
    "pronouns",
    "profile_url",
    "status",
    "is_active",
    "match_record",
    "match_wins",
    "match_losses",
    "match_draws",
    "match_count",
    "game_record",
    "game_wins",
    "game_losses",
    "game_draws",
    "game_count",
    "points",
    "opponent_match_win_pct",
    "team_game_win_pct",
    "opponent_game_win_pct",
    "final_tiebreaker",
    "opponent_count",
    "decklists",
    "decklist_ids",
    "round",
    "round_number",
    "round_id",
    "phase_name",
    "matching_method",
    "format_name",
    "format_id",
    "standings_published",
    "date_created",
    "tournament_id",
]


class ScrapeError(RuntimeError):
    """Raised when Melee.gg returns something we cannot parse."""


class MeleeScraper:
    def __init__(self, url: str, delay: float = 0.4, timeout: int = 30, retries: int = 4):
        self.url = url
        self.delay = delay
        self.timeout = timeout
        self.retries = retries
        self.tournament_id = self._parse_tournament_id(url)
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": USER_AGENT,
                "Accept-Language": "en-US,en;q=0.9",
            }
        )
        self.tournament_name: str | None = None

    # ------------------------------------------------------------------ utils
    @staticmethod
    def _parse_tournament_id(url: str) -> str:
        match = re.search(r"/Tournament/View/(\d+)", url)
        if not match:
            raise ScrapeError(f"Could not find a tournament id in URL: {url}")
        return match.group(1)

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        last_error: Exception | None = None
        for attempt in range(1, self.retries + 1):
            try:
                response = self.session.request(method, url, timeout=self.timeout, **kwargs)
                if response.status_code >= 500 or response.status_code == 429:
                    raise ScrapeError(
                        f"HTTP {response.status_code} from {url}"
                    )
                response.raise_for_status()
                return response
            except (requests.RequestException, ScrapeError) as exc:
                last_error = exc
                if attempt == self.retries:
                    break
                wait = self.delay * (2 ** (attempt - 1))
                print(f"  ! {exc} - retrying in {wait:.1f}s", file=sys.stderr)
                time.sleep(wait)
        raise ScrapeError(f"Request failed: {method} {url}: {last_error}")

    # ------------------------------------------------------------------- page
    def fetch_page(self) -> str:
        print(f"Fetching tournament page: {self.url}")
        response = self._request("GET", self.url)
        return response.text

    @staticmethod
    def parse_tournament_name(html: str) -> str | None:
        soup = BeautifulSoup(html, "html.parser")
        og_title = soup.find("meta", attrs={"property": "og:title"})
        if og_title and og_title.get("content"):
            return og_title["content"].strip()
        if soup.title and soup.title.string:
            return soup.title.string.replace("| Melee", "").strip()
        return None

    @staticmethod
    def parse_rounds(html: str) -> list[dict[str, Any]]:
        """Read the standings round selector buttons from the page."""
        soup = BeautifulSoup(html, "html.parser")
        container = soup.find(id="standings-round-selector-container")
        if not container:
            raise ScrapeError("Standings round selector not found on the page.")

        rounds: list[dict[str, Any]] = []
        for button in container.select("button.round-selector"):
            round_id = button.get("data-id")
            name = button.get("data-name")
            if not round_id or not name:
                continue
            completed = str(button.get("data-is-completed", "")).lower() == "true"
            rounds.append(
                {
                    "id": int(round_id),
                    "name": name.strip(),
                    "completed": completed,
                }
            )

        if not rounds:
            raise ScrapeError("No rounds found in the standings selector.")
        return rounds

    @staticmethod
    def pick_round(
        rounds: list[dict[str, Any]], selector: str | None
    ) -> dict[str, Any]:
        if selector:
            for rnd in rounds:
                if selector.lower() in (rnd["name"].lower(), str(rnd["id"])):
                    return rnd
            names = ", ".join(r["name"] for r in rounds)
            raise ScrapeError(
                f"Round {selector!r} not found. Available rounds: {names}"
            )

        # Mirror the site: the last completed round is selected by default.
        completed = [r for r in rounds if r["completed"]]
        return completed[-1] if completed else rounds[-1]

    # ------------------------------------------------------------------- data
    @staticmethod
    def _payload(round_id: int, start: int, length: int) -> dict[str, Any]:
        return {
            "draw": 1,
            "start": start,
            "length": length,
            "roundId": round_id,
            "columns[0][data]": "Rank",
            "columns[0][name]": "",
            "columns[0][searchable]": "true",
            "columns[0][orderable]": "true",
            "columns[0][search][value]": "",
            "columns[0][search][regex]": "false",
            "order[0][column]": 0,
            "order[0][dir]": "asc",
            "search[value]": "",
            "search[regex]": "false",
        }

    def fetch_round_standings(self, round_id: int) -> list[dict[str, Any]]:
        endpoint = f"{BASE_URL}/Standing/GetRoundStandings"
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Referer": self.url,
        }

        rows: list[dict[str, Any]] = []
        seen_ids: set[Any] = set()
        start = 0
        total: int | None = None

        while True:
            response = self._request(
                "POST",
                endpoint,
                data=self._payload(round_id, start, PAGE_SIZE),
                headers=headers,
            )
            try:
                payload = response.json()
            except ValueError as exc:
                raise ScrapeError(
                    "Melee.gg returned a non-JSON response for the standings "
                    "endpoint (the page layout may have changed)."
                ) from exc

            if payload.get("Error"):
                raise ScrapeError(
                    f"Melee.gg API error: {payload.get('Message') or payload}"
                )

            page = payload.get("data") or []
            total = payload.get("recordsTotal", total)

            for row in page:
                key = row.get("ID")
                if key is not None and key in seen_ids:
                    continue
                if key is not None:
                    seen_ids.add(key)
                rows.append(row)

            print(f"  fetched {len(rows)}/{total if total is not None else '?'} players")

            if not page or total is None or len(rows) >= total:
                break

            start += PAGE_SIZE
            if self.delay:
                time.sleep(self.delay)

        return rows

    # ---------------------------------------------------------------- flatten
    def flatten(
        self, standings: Iterable[dict[str, Any]], round_info: dict[str, Any]
    ) -> list[dict[str, Any]]:
        players: list[dict[str, Any]] = []

        for row in standings:
            team = row.get("Team") or {}
            team_players = team.get("Players") or [{}]
            decklists = row.get("Decklists") or []

            for player in team_players:
                username = player.get("Username")
                players.append(
                    {
                        "rank": row.get("Rank"),
                        "player_id": player.get("ID"),
                        "team_id": row.get("TeamId") or team.get("ID"),
                        "display_name": player.get("DisplayName"),
                        "name_last_first": player.get("DisplayNameLastFirst"),
                        "username": username,
                        "pronouns": player.get("PronounsDescription"),
                        "profile_url": (
                            f"{BASE_URL}/Profile/Index/{username}" if username else None
                        ),
                        "status": team.get("StatusDescription"),
                        "is_active": team.get("IsActive"),
                        "match_record": row.get("MatchRecord"),
                        "match_wins": row.get("MatchWins"),
                        "match_losses": row.get("MatchLosses"),
                        "match_draws": row.get("MatchDraws"),
                        "match_count": row.get("MatchCount"),
                        "game_record": row.get("GameRecord"),
                        "game_wins": row.get("GameWins"),
                        "game_losses": row.get("GameLosses"),
                        "game_draws": row.get("GameDraws"),
                        "game_count": row.get("GameCount"),
                        "points": row.get("Points"),
                        "opponent_match_win_pct": row.get(
                            "OpponentMatchWinPercentage"
                        ),
                        "team_game_win_pct": row.get("TeamGameWinPercentage"),
                        "opponent_game_win_pct": row.get(
                            "OpponentGameWinPercentage"
                        ),
                        "final_tiebreaker": row.get("FinalTiebreaker"),
                        "opponent_count": row.get("OpponentCount"),
                        "decklists": "; ".join(
                            d.get("DecklistName") or "" for d in decklists
                        )
                        or None,
                        "decklist_ids": "; ".join(
                            str(d.get("DecklistId")) for d in decklists if d.get("DecklistId")
                        )
                        or None,
                        "round": row.get("Round") or round_info["name"],
                        "round_number": row.get("RoundNumber"),
                        "round_id": row.get("RoundId") or round_info["id"],
                        "phase_name": row.get("PhaseName"),
                        "matching_method": row.get("MatchingMethodDescription"),
                        "format_name": row.get("FormatName"),
                        "format_id": row.get("FormatId"),
                        "standings_published": row.get("StandingsPublished"),
                        "date_created": row.get("DateCreated"),
                        "tournament_id": row.get("TournamentId")
                        or int(self.tournament_id),
                    }
                )

        return players

    # ------------------------------------------------------------------- main
    def run(self, round_selector: str | None = None) -> dict[str, Any]:
        html = self.fetch_page()
        self.tournament_name = self.parse_tournament_name(html)
        rounds = self.parse_rounds(html)
        selected = self.pick_round(rounds, round_selector)

        print(
            f"Tournament: {self.tournament_name or self.tournament_id} "
            f"({self.tournament_id})"
        )
        print(
            f"Round: {selected['name']} (id={selected['id']}, "
            f"completed={selected['completed']})"
        )

        standings = self.fetch_round_standings(selected["id"])
        players = self.flatten(standings, selected)

        return {
            "tournament": {
                "id": int(self.tournament_id),
                "name": self.tournament_name,
                "url": self.url,
                "round": selected,
                "scraped_at": datetime.now(timezone.utc).isoformat(),
            },
            "player_count": len(players),
            "players": players,
        }


def write_json(path: Path, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")


def write_csv(path: Path, players: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(players)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Scrape all player standings from a Melee.gg tournament."
    )
    parser.add_argument(
        "url",
        help="Tournament URL, e.g. https://melee.gg/Tournament/View/491103#standings",
    )
    parser.add_argument(
        "-o",
        "--output",
        default="players",
        help="Output file prefix (default: players -> players.json, players.csv)",
    )
    parser.add_argument(
        "--round",
        dest="round_selector",
        help="Round name or id to scrape (default: last completed round)",
    )
    parser.add_argument(
        "--no-csv", action="store_true", help="Only write the JSON file"
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=0.4,
        help="Delay in seconds between paginated requests (default: 0.4)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        scraper = MeleeScraper(args.url, delay=args.delay)
        result = scraper.run(args.round_selector)
    except ScrapeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    output = Path(args.output)
    json_path = output.with_suffix(".json")
    write_json(json_path, result)
    print(f"Wrote {result['player_count']} players to {json_path}")

    if not args.no_csv:
        csv_path = output.with_suffix(".csv")
        write_csv(csv_path, result["players"])
        print(f"Wrote {result['player_count']} players to {csv_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
