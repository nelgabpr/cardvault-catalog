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


def request_json(
    url: str,
    api_key: str | None,
    *,
    attempts: int = 5,
    timeout: int = 60,
) -> dict:
    request = urllib.request.Request(url)
    if api_key:
        request.add_header("X-Api-Key", api_key)
    request.add_header("User-Agent", "CardVaultAI-CatalogBuilder/1.0")
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            if error.code not in {429, 500, 502, 503, 504} or attempt == attempts - 1:
                raise
            retry_after = error.headers.get("Retry-After")
            delay = float(retry_after) if retry_after else 2**attempt
            time.sleep(min(delay, 30))
        except urllib.error.URLError:
            if attempt == attempts - 1:
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


def request_set_prices(set_id: str, api_key: str | None) -> dict[str, dict]:
    query = urllib.parse.urlencode(
        {
            "q": f"set.id:{set_id}",
            "page": 1,
            "pageSize": 250,
            "select": "id,tcgplayer",
        }
    )
    first = request_json(f"{API_URL}?{query}", api_key, attempts=2, timeout=25)
    pages = [first]
    page_count = (int(first["totalCount"]) + 249) // 250
    for page in range(2, page_count + 1):
        page_query = urllib.parse.urlencode(
            {
                "q": f"set.id:{set_id}",
                "page": page,
                "pageSize": 250,
                "select": "id,tcgplayer",
            }
        )
        pages.append(request_json(f"{API_URL}?{page_query}", api_key, attempts=2, timeout=25))
    return {
        str(card["id"]): card.get("tcgplayer") or {}
        for response in pages
        for card in response["data"]
    }


def enrich_tcgplayer_prices(raw_cards: list[dict], set_ids: list[str], api_key: str | None) -> int:
    prices_by_id: dict[str, dict] = {}
    failed_sets: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(request_set_prices, set_id, api_key): set_id
            for set_id in set_ids
        }
        for completed, future in enumerate(concurrent.futures.as_completed(futures), start=1):
            set_id = futures[future]
            try:
                prices_by_id.update(future.result())
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
                failed_sets.append(set_id)
            print(f"Fetched prices for {completed}/{len(set_ids)} sets", file=sys.stderr)

    for raw in raw_cards:
        tcgplayer = prices_by_id.get(str(raw["id"]))
        if tcgplayer:
            raw["tcgplayer"] = tcgplayer
    if failed_sets:
        print(
            f"Pricing unavailable for {len(failed_sets)} sets; existing app prices will be preserved",
            file=sys.stderr,
        )
    return sum(1 for raw in raw_cards if raw.get("tcgplayer"))


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
    rarity = str(raw.get("rarity") or raw.get("variant") or "").lower()
    premium_rarity = any(
        marker in rarity
        for marker in ("holo", "illustration", "ultra", "secret", "rainbow", "shiny", "full art")
    )
    preferred_price_keys = (
        ("holofoil", "reverseHolofoil", "normal", "1stEditionHolofoil", "unlimitedHolofoil")
        if premium_rarity
        else ("normal", "reverseHolofoil", "holofoil", "1stEditionNormal", "1stEditionHolofoil", "unlimited")
    )
    remaining_price_keys = tuple(sorted(set(price_groups) - set(preferred_price_keys)))
    ordered_price_keys = preferred_price_keys + remaining_price_keys
    price_options = []
    market_price = None
    price_finish = None
    supported_fields = ("low", "mid", "high", "market", "directLow")
    for price_key in ordered_price_keys:
        raw_option = price_groups.get(price_key) or {}
        option = {"finish": price_key}
        for field in supported_fields:
            candidate = raw_option.get(field)
            if isinstance(candidate, (int, float)) and candidate >= 0:
                option[field] = round(float(candidate), 2)
        if len(option) == 1:
            continue
        price_options.append(option)
        if market_price is None and option.get("market") is not None:
            market_price = option["market"]
            price_finish = price_key
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
        card["priceFinish"] = price_finish
    if price_options:
        card["priceOptions"] = price_options
    if tcgplayer.get("url"):
        card["pricingURL"] = str(tcgplayer["url"])
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
    parser.add_argument("--sets-file", type=pathlib.Path)
    parser.add_argument("--enrich-prices", action="store_true")
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
        if args.sets_file:
            raw_sets = json.loads(args.sets_file.read_text())
            sets_by_id = {str(card_set["id"]): card_set for card_set in raw_sets}
        else:
            sets_by_id = None
        if args.enrich_prices:
            set_ids = sorted(sets_by_id or {str(raw["id"]).rsplit("-", 1)[0]: {} for raw in raw_cards})
            priced_count = enrich_tcgplayer_prices(
                raw_cards,
                set_ids,
                os.environ.get("POKEMON_TCG_API_KEY"),
            )
            print(f"Enriched {priced_count}/{total_count} cards with market data", file=sys.stderr)
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
    image_count = sum(bool(card.get("imageURL")) for card in cards)
    priced_count = sum(card.get("marketPrice") is not None for card in cards)
    price_option_count = sum(len(card.get("priceOptions") or []) for card in cards)
    languages = sorted({str(card.get("language") or "English") for card in cards})
    manifest = {
        "schemaVersion": 1,
        "catalogVersion": now.strftime("%Y.%m.%d.%H%M"),
        "generatedAt": now.isoformat().replace("+00:00", "Z"),
        "cardCount": len(cards),
        "sha256": digest,
        "minimumAppVersion": "1.0",
        "catalogPath": args.catalog_path,
        "source": "Pokemon TCG Data + Pokemon TCG API V2",
        "sourceRevision": now.isoformat().replace("+00:00", "Z"),
        "imageCardCount": image_count,
        "pricedCardCount": priced_count,
        "priceOptionCount": price_option_count,
        "pricingSource": "TCGplayer market by finish",
        "languages": languages,
    }

    args.catalog.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.parent.mkdir(parents=True, exist_ok=True)
    args.catalog.write_bytes(catalog_data)
    args.manifest.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    print(f"Wrote {len(cards)} cards, SHA-256 {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
