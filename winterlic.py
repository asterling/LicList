"""Fetch and persist the current Toronto Winterlicious / Summerlicious menus.

Toronto's open-data endpoint is the same year-round; the city swaps the active
campaign in place (Winterlicious in Jan/Feb, Summerlicious in Jul/Aug). This
script writes three files so the frontend can stay generic:

  - menus-latest.json                  always the freshest snapshot
  - menus-{Winter,Summer}licious-YYYY.json  per-campaign archive
  - season.json                        metadata (label, season, year)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

API_URL = "https://secure.toronto.ca/c3api_data/v2/DataAccess.svc/Licious/map_data"
# OData defaults to Atom XML; ask for JSON explicitly. The TLS impersonation
# (see fetch_all), not these headers, is what satisfies the WAF.
HEADERS = {"Accept": "application/json"}
IMPERSONATE = "chrome"

HERE = Path(__file__).parent


def fetch_all(top: int = 1000) -> list:
    # curl_cffi (not plain requests): in late May 2026 the City's Akamai Bot
    # Manager started 403-ing the endpoint based on the client's TLS/JA3
    # fingerprint, not its IP or headers — a real Chrome from any IP still
    # works, but stock requests/curl get blocked everywhere. curl_cffi replays
    # Chrome's actual TLS handshake (impersonate="chrome") to get back through.
    # Imported lazily so --no-fetch enrichment runs don't need the dependency.
    from curl_cffi import requests

    resp = requests.get(
        API_URL,
        params={"$skip": 0, "$top": top},
        headers=HEADERS,
        impersonate=IMPERSONATE,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("value", [])


# Best-effort festival dates per year. Update these when the City publishes
# the dates for a new edition; otherwise the calendar export silently no-ops.
FESTIVAL_DATES = {
    ("Winterlicious", 2026): ("2026-01-30", "2026-02-12"),
    ("Summerlicious", 2026): ("2026-07-03", "2026-07-16"),
    ("Winterlicious", 2027): ("2027-01-29", "2027-02-11"),
}


def detect_season(today: datetime | None = None) -> tuple[str, int]:
    """Pick the campaign that is most relevant right now.

    Override with LICIOUS_SEASON=Winter|Summer if you need to force one.
    """
    override = os.getenv("LICIOUS_SEASON", "").strip().lower()
    today = today or datetime.utcnow()
    if override in ("winter", "winterlicious"):
        return ("Winterlicious", today.year)
    if override in ("summer", "summerlicious"):
        return ("Summerlicious", today.year)

    # Promote the edition that is currently running or next on the calendar.
    # Winterlicious runs late Jan–mid Feb; Summerlicious runs early–mid July.
    #   Jan–Feb  -> Winterlicious (this year)
    #   Mar–Sep  -> Summerlicious (this year)  ← covers the summer run-up + run
    #   Oct–Dec  -> Winterlicious (next year)  ← the winter edition lands in Jan
    month = today.month
    if 3 <= month <= 9:
        return ("Summerlicious", today.year)
    if month >= 10:
        return ("Winterlicious", today.year + 1)
    return ("Winterlicious", today.year)


def _name_key(name: str | None) -> str:
    """Normalize a restaurant name for cross-campaign comparison."""
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def prior_other_season(season: str, year: int) -> tuple[str, int]:
    """The opposite-season edition that ran most recently before this one.

    Summerlicious follows that year's Winterlicious; Winterlicious follows the
    previous year's Summerlicious.
    """
    if season == "Summerlicious":
        return ("Winterlicious", year)
    return ("Summerlicious", year - 1)


def mark_new_this_season(restaurants: list, season: str, year: int) -> int:
    """Tag each restaurant with `new_this_season` by diffing names against the
    previous opposite-season archive. Returns how many were flagged new.

    If the prior archive is missing or implausibly small (e.g. the City API
    served a near-empty set after a festival ended), we skip tagging rather
    than flag the entire roster as "new".
    """
    other_season, other_year = prior_other_season(season, year)
    archive = HERE / f"menus-{other_season}-{other_year}.json"
    prev_keys: set[str] = set()
    if archive.exists():
        try:
            prev = json.loads(archive.read_text(encoding="utf-8"))
            prev_keys = {_name_key(r.get("restaurant_name")) for r in prev}
        except (json.JSONDecodeError, OSError):
            prev_keys = set()
    if len(prev_keys) < 20:
        print(
            f"Skipping new-this-season tagging: prior archive "
            f"{archive.name} missing or too small ({len(prev_keys)} names)."
        )
        for r in restaurants:
            r.pop("new_this_season", None)
        return 0
    new_count = 0
    for r in restaurants:
        is_new = _name_key(r.get("restaurant_name")) not in prev_keys
        r["new_this_season"] = is_new
        new_count += is_new
    return new_count


def write_json(path: Path, payload) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch (and optionally enrich) Toronto Licious menus.")
    parser.add_argument("--enrich", action="store_true", help="Run local LLM enrichment after fetching.")
    parser.add_argument("--no-fetch", action="store_true", help="Skip fetch; only run enrichment on existing JSON.")
    parser.add_argument("--model", default="qwen2.5:7b", help="Ollama model tag for enrichment.")
    parser.add_argument("--limit", type=int, default=None, help="Enrich only the first N restaurants (testing).")
    parser.add_argument(
        "--allow-ai-loss",
        action="store_true",
        help="Skip the safety check that aborts when a fetch would wipe out prior AI enrichment.",
    )
    args = parser.parse_args()

    if not args.no_fetch:
        restaurants = fetch_all()
        if not restaurants:
            print("No data returned from API.")
            return
    else:
        restaurants = None  # set below from existing file

    season, year = detect_season()
    label = f"{season} {year}"
    dates = FESTIVAL_DATES.get((season, year))

    latest_path = HERE / "menus-latest.json"
    archive_path = HERE / f"menus-{season}-{year}.json"

    if restaurants is not None:
        # Carry over AI enrichment from the previous snapshot so a fresh fetch
        # without --enrich doesn't wipe months of LLM work. enrich.py's cache
        # will re-prompt only when a restaurant's content hash changes.
        prev_ai_count = 0
        if latest_path.exists():
            try:
                prev = json.loads(latest_path.read_text(encoding="utf-8"))
                prev_ai = {p.get("id"): p.get("ai") for p in prev if p.get("ai")}
                prev_ai_count = len(prev_ai)
                # Carry over the kids-menu tag from kids_menu.py the same way.
                prev_kids = {p.get("id") for p in prev if p.get("kids_menu")}
                if prev_kids:
                    kept = 0
                    for r in restaurants:
                        if r.get("id") in prev_kids:
                            r["kids_menu"] = True
                            kept += 1
                    print(f"Preserved kids-menu tag for {kept}/{len(prev_kids)} restaurants.")
                if prev_ai:
                    preserved = 0
                    for r in restaurants:
                        ai = prev_ai.get(r.get("id"))
                        if ai:
                            r["ai"] = ai
                            preserved += 1
                    print(f"Preserved AI enrichment for {preserved}/{prev_ai_count} restaurants.")
                    if (
                        preserved == 0
                        and prev_ai_count >= 10
                        and not args.allow_ai_loss
                    ):
                        sys.exit(
                            f"ABORT: prior snapshot had {prev_ai_count} AI-enriched "
                            "restaurants but none of their ids appear in the fresh "
                            "fetch. This usually means the City API changed ids or "
                            "the prior file is corrupt. Pass --allow-ai-loss to "
                            "override after you've confirmed it's intentional."
                        )
            except (json.JSONDecodeError, OSError) as e:
                print(f"Warning: could not preserve prior AI fields ({e}).")

        new_count = mark_new_this_season(restaurants, season, year)
        if new_count:
            print(f"Flagged {new_count} restaurants new this season.")

        write_json(latest_path, restaurants)
        write_json(archive_path, restaurants)
        season_payload = {
            "season": season,
            "year": year,
            "label": label,
            "fetched_at": datetime.utcnow().isoformat() + "Z",
        }
        if dates:
            start, end = dates
            season_payload["dates_start"] = start
            season_payload["dates_end"] = end
        write_json(HERE / "season.json", season_payload)
        print(f"Saved {len(restaurants)} restaurants for {label}.")

    if args.enrich or args.no_fetch:
        from enrich import enrich_restaurants
        if not latest_path.exists():
            print(f"Cannot enrich: {latest_path} is missing. Drop --no-fetch.")
            return
        cache_path = HERE / "enrichment-cache.json"
        calls, hits, failures = enrich_restaurants(
            latest_path, cache_path, args.model, limit=args.limit
        )
        print(f"Enrichment: {calls} new, {hits} cached, {failures} failed.")
        # Mirror enriched data into the per-season archive so both stay in sync.
        if archive_path.exists():
            archive_path.write_bytes(latest_path.read_bytes())


if __name__ == "__main__":
    main()
