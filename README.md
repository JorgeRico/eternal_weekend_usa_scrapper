# Melee.gg Tournament Scraper

Scrapes every player's standings from a Melee.gg tournament and exports JSON + CSV.

The standings table on Melee.gg is rendered client-side, but the page exposes the
round list in its HTML and the table data through a JSON endpoint:

```
POST https://melee.gg/Standing/GetRoundStandings
```

This scraper uses `requests` (no Selenium/browser needed), auto-detects the
tournament's rounds, selects the last completed round (the same one the site
shows by default), and paginates the endpoint 500 rows at a time.

## Project layout

```
all/                  scraper.py + standings output (JSON/CSV)
decklists/            decklist_counts.py + decklist leaderboard
cards/                card_counts.py + maindeck/sideboard leaderboards
.cache/decklists/     cached decklist HTML (created by card_counts.py)
requirements.txt      Python dependencies
```

Each script is standalone and runs from its own folder.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

```bash
cd all

# default output: players.json / players.csv
python3 scraper.py "https://melee.gg/Tournament/View/491103#standings"

# custom output name: eternal_weekend.json / eternal_weekend.csv
python3 scraper.py "https://melee.gg/Tournament/View/491103#standings" -o eternal_weekend

# scrape a specific round by name or id
python3 scraper.py "https://melee.gg/Tournament/View/491103#standings" --round "Round 14"

# JSON only
python3 scraper.py "https://melee.gg/Tournament/View/491103#standings" --no-csv
```

Run `python3 scraper.py -h` for all options.

## Count repeated decklists

`decklist_counts.py` groups a results CSV by the `decklists` column and writes a
new CSV with one row per decklist and its player count:

```bash
cd decklists
python3 decklist_counts.py ../all/eternal_weekend_legacy.csv -o decklist_counts.csv
```

Output (`decklist_counts.csv`):

```csv
decklist,count
W-U-B-G Aluren,58
Dimir Tempo,47
Bant Beanstalk,46
```

The same logic is available as a function:

```python
from decklist_counts import count_decklists

results = count_decklists(
    "../all/eternal_weekend_legacy.csv",
    "decklist_counts.csv",
)
# [('W-U-B-G Aluren', 58), ('Dimir Tempo', 47), ...]
```

Options:

- `--column` – decklist column name (default: `decklists`)
- `--no-split` – treat multi-decklist values as one group instead of splitting on `;`
- `--keep-empty` – include blank decklists as an empty-name group

## Most played maindeck and sideboard cards

`card_counts.py` reads the `decklist_ids` column from a results CSV, downloads
each decklist once (cached under `.cache/decklists`), and writes:

- `maindeck_top100.csv`
- `sideboard_top100.csv`

```bash
cd cards
python3 card_counts.py ../all/eternal_weekend_legacy.csv --cache-dir ../.cache/decklists
```

Each output has `card,copies,decks,avg_copies`, sorted by total copies:

```csv
card,copies,decks,avg_copies
Force of Will,2355,589,4.0
Brainstorm,2151,538,4.0
Ponder,1941,515,3.77
```

The same logic is available as functions:

```python
from card_counts import (
    read_decklist_ids,
    load_decklists,
    most_played_maindeck_cards,
    most_played_sideboard_cards,
)

ids = read_decklist_ids("../all/eternal_weekend_legacy.csv")
decklists, failures = load_decklists(ids, cache_dir="../.cache/decklists")

maindeck = most_played_maindeck_cards(decklists, limit=100)
sideboard = most_played_sideboard_cards(decklists, limit=100)
```

Useful options:

- `--limit N` – cards per leaderboard (default: 100)
- `--board maindeck|sideboard|both` – which leaderboard(s) to write
- `--workers N` – concurrent downloads (default: 6)
- `--refresh` – ignore the cache and refetch decklists
- `--cache-dir PATH` – cache location (default: `.cache/decklists`)

Decklists are parsed from Melee's `#decklist-text` export, with the categorized
HTML view as a fallback. `copies` is the total number of cards across all
decklists; `decks` is the number of decklists that include at least one copy.

## Output fields

Each player record includes:

| Field | Description |
| --- | --- |
| `rank` | Final standing / rank |
| `player_id`, `team_id` | Melee.gg player and team ids |
| `display_name`, `name_last_first`, `username` | Player names |
| `pronouns` | Pronouns, when published |
| `profile_url` | Melee.gg profile URL |
| `status`, `is_active` | Active/eliminated status |
| `match_record`, `match_wins/losses/draws` | Match results |
| `game_record`, `game_wins/losses/draws` | Game results |
| `points` | Tournament points |
| `opponent_match_win_pct`, `team_game_win_pct`, `opponent_game_win_pct` | Tiebreakers (OMW%, TGW%, OGW%) |
| `final_tiebreaker`, `opponent_count` | Remaining tiebreaker data |
| `decklists`, `decklist_ids` | Decklist names and ids |
| `round`, `round_number`, `round_id`, `phase_name` | Round metadata |
| `matching_method`, `format_name`, `format_id` | Format metadata |
| `standings_published`, `date_created`, `tournament_id` | Extra metadata |

## Notes

- The endpoint caps responses at 500 rows; the scraper handles pagination.
- A short delay (`--delay`, default 0.4s) is applied between page requests.
- Round data can change while an event is running. Re-run the scraper to refresh.
- If Melee.gg changes its page/API structure, update `parse_rounds()` and
  `_payload()` accordingly.
