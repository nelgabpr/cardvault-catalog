#!/usr/bin/env python3
"""Build CardVault AI's deterministic offline catalog from Pokémon TCG API V2."""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API_URL = "https://api.pokemontcg.io/v2/cards"
SETS_API_URL = "https://api.pokemontcg.io/v2/sets"
PAGE_SIZE = 100
SET_PAGE_SIZE = 250
# Requesting the nested `set` object through the cards collection endpoint can
# fail or return incomplete data. Fetch compact card rows and join official set
# metadata separately by the stable set prefix in each card ID.
SELECT = "id,name,number,rarity,types,subtypes,images,tcgplayer"


def request_json(url: str, api_key: str | None) -> dict:
    request = urllib.request.Request(url)
    if api_key:
        request.add_header("X-Api-Key", api_key)
    request.add_header("User-Agent", "CardVaultAI-CatalogBuilder/1.0")
    for attempt in range(5):
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code not in {429, 500, 502, 503, 504} or attempt == 4:
                raise
            retry_after = error.headers.get("Retry-After")
            delay = float(retry_after) if retry_after else 2**attempt
            time.sleep(min(delay, 30))
        except urllib.error.URLError:
            if attempt == 4:
                raise
            time.sleep(2**attempt)
    raise RuntimeError(f"Unable to fetch {url}")


def request_page(page: int, api_key: str | None) -> dict:
    query = urllib.parse.urlencode({"page": page, "pageSize": PAGE_SIZE, "select": SELECT})
    return request_json(f"{API_URL}?{query}", api_key)


def request_sets(api_key: str | None) -> dict[str, dict]:
    query = urllib.parse.urlencode({"page": 1, "pageSize": SET_PAGE_SIZE})
    response = request_json(f"{SETS_API_URL}?{query}", api_key)
    total_count = int(response["totalCount"])
    sets = response["data"]
    if len(sets) != total_count:
        raise RuntimeError(f"Expected {total_count} sets, received {len(sets)}")
    return {str(card_set["id"]): card_set for card_set in sets}


def normalize_card(raw: dict, sets_by_id: dict[str, dict] | None = None) -> dict:
    embedded_set = raw.get("set") or {}
    card_id = str(raw["id"])
    set_id = str(embedded_set.get("id") or card_id.rsplit("-", 1)[0]).strip()
    card_set = embedded_set or (sets_by_id or {}).get(set_id, {})
    number = str(raw.get("number") or raw.get("collectorNumber") or "").strip()
    if "/" in number:
        number = number.split("/", 1)[0]
    printed_total = card_set.get("printedTotal")
    collector_number = number
    if number and printed_total and "/" not in number:
        collector_number = f"{number}/{printed_total}"

    set_code = str(card_set.get("ptcgoCode") or set_id).strip()
    subtypes = [str(value) for value in raw.get("subtypes") or raw.get("aliases") or []]
    aliases = sorted({value for value in [set_id, set_code, *subtypes] if value})

    tcgplayer = raw.get("tcgplayer") or {}
    price_groups = tcgplayer.get("prices") or {}
    preferred_price_keys = ("holofoil", "reverseHolofoil", "normal", "1stEditionHolofoil", "unlimitedHolofoil")
    market_price = None
    for price_key in preferred_price_keys:
        candidate = (price_groups.get(price_key) or {}).get("market")
        if isinstance(candidate, (int, float)) and candidate >= 0:
            market_price = round(float(candidate), 2)
            break
    images = raw.get("images") or {}

    card = {
        "id": card_id,
        "name": str(raw["name"]),
        "setName": str(card_set.get("name") or "Unknown Set"),
        "setCode": set_code,
        "collectorNumber": collector_number,
        "language": str(raw.get("language") or "English"),
        "variant": str(raw.get("rarity") or raw.get("variant") or (subtypes[-1] if subtypes else "Standard")),
        "type": str((raw.get("types") or [raw.get("type") or "Colorless"])[0]),
        "aliases": aliases,
    }
    if images.get("small"):
        card["imageURL"] = str(images["small"])
    if images.get("large"):
        card["largeImageURL"] = str(images["large"])
    if market_price is not None:
        card["marketPrice"] = market_price
        card["priceSource"] = "TCGplayer market"
    if tcgplayer.get("updatedAt"):
        card["priceUpdatedAt"] = str(tcgplayer["updatedAt"])
    return card


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", required=True, type=pathlib.Path)
    parser.add_argument("--manifest", required=True, type=pathlib.Path)
    parser.add_argument("--catalog-path", default="LocalCardCatalog.json")
    parser.add_argument("--source-directory", type=pathlib.Path)
    parser.add_argument("--source-catalog", type=pathlib.Path)
    args = parser.parse_args()

    if args.source_directory and args.source_catalog:
        parser.error("--source-directory and --source-catalog are mutually exclusive")

    if args.source_catalog:
        raw_cards = json.loads(args.source_catalog.read_text())
        total_count = len(raw_cards)
        sets_by_id = request_sets(os.environ.get("POKEMON_TCG_API_KEY"))
    elif args.source_directory:
        source_files = sorted(args.source_directory.glob("*.json"))
        if not source_files:
            raise RuntimeError("No JSON card files were found in the source directory")
        raw_cards = [raw for path in source_files for raw in json.loads(path.read_text())]
        total_count = len(raw_cards)
        sets_by_id = None
    else:
        api_key = os.environ.get("POKEMON_TCG_API_KEY")
        sets_by_id = request_sets(api_key)
        first = request_page(1, api_key)
        total_count = int(first["totalCount"])
        page_count = (total_count + PAGE_SIZE - 1) // PAGE_SIZE
        pages: dict[int, dict] = {1: first}

        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as executor:
            futures = {
                executor.submit(request_page, page, api_key): page
                for page in range(2, page_count + 1)
            }
            for completed, future in enumerate(concurrent.futures.as_completed(futures), start=2):
                page = futures[future]
                pages[page] = future.result()
                print(f"Fetched {completed}/{page_count} pages", file=sys.stderr)
        raw_cards = [raw for page in range(1, page_count + 1) for raw in pages[page]["data"]]

    cards = [normalize_card(raw, sets_by_id) for raw in raw_cards]
    cards.sort(key=lambda card: card["id"])
    if len(cards) != total_count or len({card["id"] for card in cards}) != len(cards):
        raise RuntimeError("Catalog count or stable-ID uniqueness validation failed")
    if sets_by_id:
        missing_sets = [card["id"] for card in cards if card["setName"] == "Unknown Set"]
        if missing_sets:
            sample = ", ".join(missing_sets[:5])
            raise RuntimeError(f"Set metadata missing for {len(missing_sets)} cards: {sample}")

    catalog_data = json.dumps(cards, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    digest = hashlib.sha256(catalog_data).hexdigest()
    if args.manifest.exists():
        existing = json.loads(args.manifest.read_text())
        if existing.get("sha256") == digest:
            print(f"Catalog unchanged: {len(cards)} cards, SHA-256 {digest}")
            return 0

    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    manifest = {
        "schemaVersion": 1,
        "catalogVersion": now.strftime("%Y.%m.%d.%H%M"),
        "generatedAt": now.isoformat().replace("+00:00", "Z"),
        "cardCount": len(cards),
        "sha256": digest,
        "minimumAppVersion": "1.0",
        "catalogPath": args.catalog_path,
        "source": "Pokémon TCG API V2",
        "sourceRevision": now.isoformat().replace("+00:00", "Z"),
    }

    args.catalog.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.catalog.write_bytes(catalog_data)
    args.manifest.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    print(f"Wrote {len(cards)} cards, SHA-256 {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
