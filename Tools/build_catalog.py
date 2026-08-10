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
PRICE_FIELDS = (
    "marketPrice",
    "priceSource",
    "priceUpdatedAt",
    "priceFinish",
    "priceOptions",
    "pricingURL",
)
IMAGE_FIELDS = ("imageURL", "largeImageURL")


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
        "providerIDs": {"pokemonTCG": card_id},
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


def parse_provider_date(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    normalized = str(value).strip().replace("/", "-")
    if len(normalized) == 10:
        normalized += "T00:00:00+00:00"
    elif normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = dt.datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def carry_forward_last_known_good(
    cards: list[dict],
    previous_cards: list[dict],
    *,
    now: dt.datetime,
    max_price_age_days: int,
) -> dict[str, int]:
    """Preserve still-relevant provider data when a refresh is incomplete.

    Current provider values always win. Previous prices retain their original
    timestamp so the app can label them stale instead of presenting them as new.
    """
    previous_by_id = {str(card.get("id")): card for card in previous_cards}
    cutoff = now - dt.timedelta(days=max_price_age_days)
    carried_price_cards = 0
    carried_price_options = 0
    carried_artwork_cards = 0

    for card in cards:
        previous = previous_by_id.get(str(card["id"]))
        if not previous:
            continue

        copied_artwork = False
        for field in IMAGE_FIELDS:
            if not card.get(field) and previous.get(field):
                card[field] = previous[field]
                copied_artwork = True
        if copied_artwork:
            carried_artwork_cards += 1

        previous_date = parse_provider_date(previous.get("priceUpdatedAt"))
        if previous_date is None or previous_date < cutoff:
            continue

        copied_primary_price = False
        current_options = {
            str(option.get("finish")): option
            for option in card.get("priceOptions") or []
            if option.get("finish")
        }
        for option in previous.get("priceOptions") or []:
            finish = str(option.get("finish") or "")
            if finish and finish not in current_options:
                current_options[finish] = option
                carried_price_options += 1
        if current_options:
            card["priceOptions"] = list(current_options.values())

        if card.get("marketPrice") is None and previous.get("marketPrice") is not None:
            for field in PRICE_FIELDS:
                if field == "priceOptions":
                    continue
                if not card.get(field) and previous.get(field) is not None:
                    card[field] = previous[field]
            copied_primary_price = True
        elif not card.get("pricingURL") and previous.get("pricingURL"):
            card["pricingURL"] = previous["pricingURL"]

        if copied_primary_price:
            carried_price_cards += 1

    return {
        "carriedForwardPriceCardCount": carried_price_cards,
        "carriedForwardPriceOptionCount": carried_price_options,
        "carriedForwardArtworkCardCount": carried_artwork_cards,
    }


def build_health_report(
    cards: list[dict],
    previous_cards: list[dict],
    carry_stats: dict[str, int],
    *,
    generated_at: dt.datetime,
) -> dict:
    card_count = len(cards)
    previous_count = len(previous_cards)
    priced_count = sum(card.get("marketPrice") is not None for card in cards)
    fresh_priced_count = max(0, priced_count - carry_stats["carriedForwardPriceCardCount"])
    artwork_count = sum(bool(card.get("imageURL") or card.get("largeImageURL")) for card in cards)
    previous_priced_count = sum(card.get("marketPrice") is not None for card in previous_cards)
    previous_artwork_count = sum(
        bool(card.get("imageURL") or card.get("largeImageURL")) for card in previous_cards
    )
    warnings: list[str] = []
    errors: list[str] = []

    if previous_count and card_count < previous_count * 0.95:
        errors.append(
            f"Card count fell from {previous_count} to {card_count}, exceeding the 5% safety limit."
        )
    if previous_artwork_count and artwork_count < previous_artwork_count * 0.98:
        errors.append(
            f"Artwork coverage fell from {previous_artwork_count} to {artwork_count}."
        )
    if previous_priced_count:
        previous_coverage = previous_priced_count / max(previous_count, 1)
        current_coverage = priced_count / max(card_count, 1)
        if current_coverage + 0.02 < previous_coverage:
            errors.append(
                "Price coverage fell by more than two percentage points after last-known-good merging."
            )
    if carry_stats["carriedForwardPriceCardCount"] or carry_stats["carriedForwardPriceOptionCount"]:
        warnings.append(
            "Preserved last-known provider data after an incomplete refresh: "
            f"{carry_stats['carriedForwardPriceCardCount']} primary card prices and "
            f"{carry_stats['carriedForwardPriceOptionCount']} finish-specific options."
        )

    return {
        "status": "failed" if errors else ("degraded" if warnings else "healthy"),
        "generatedAt": generated_at.isoformat().replace("+00:00", "Z"),
        "cardCount": card_count,
        "previousCardCount": previous_count,
        "artworkCardCount": artwork_count,
        "freshPricedCardCount": fresh_priced_count,
        "pricedCardCount": priced_count,
        "priceCoveragePercent": round(priced_count / max(card_count, 1) * 100, 2),
        "priceOptionCount": sum(len(card.get("priceOptions") or []) for card in cards),
        **carry_stats,
        "warnings": warnings,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--catalog", required=True, type=pathlib.Path)
    parser.add_argument("--manifest", required=True, type=pathlib.Path)
    parser.add_argument("--catalog-path", default="LocalCardCatalog.json")
    parser.add_argument("--source-directory", type=pathlib.Path)
    parser.add_argument("--source-catalog", type=pathlib.Path)
    parser.add_argument("--sets-file", type=pathlib.Path)
    parser.add_argument("--enrich-prices", action="store_true")
    parser.add_argument("--previous-catalog", type=pathlib.Path)
    parser.add_argument("--health-report", type=pathlib.Path)
    parser.add_argument("--max-price-age-days", type=int, default=90)
    parser.add_argument("--source-revision")
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

    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    previous_cards: list[dict] = []
    if args.previous_catalog and args.previous_catalog.exists():
        previous_cards = json.loads(args.previous_catalog.read_text())

    cards = [normalize_card(raw, sets_by_id) for raw in raw_cards]
    cards.sort(key=lambda card: card["id"])
    if len(cards) != total_count or len({card["id"] for card in cards}) != len(cards):
        raise RuntimeError("Catalog count or stable-ID uniqueness validation failed")
    if sets_by_id:
        missing_sets = [card["id"] for card in cards if card["setName"] == "Unknown Set"]
        if missing_sets:
            sample = ", ".join(missing_sets[:5])
            raise RuntimeError(f"Set metadata missing for {len(missing_sets)} cards: {sample}")

    carry_stats = carry_forward_last_known_good(
        cards,
        previous_cards,
        now=now,
        max_price_age_days=args.max_price_age_days,
    )
    health = build_health_report(
        cards,
        previous_cards,
        carry_stats,
        generated_at=now,
    )
    if args.health_report:
        args.health_report.parent.mkdir(parents=True, exist_ok=True)
        args.health_report.write_text(json.dumps(health, indent=2) + "\n")
    if health["errors"]:
        raise RuntimeError("Catalog health validation failed: " + " ".join(health["errors"]))

    catalog_data = json.dumps(cards, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    digest = hashlib.sha256(catalog_data).hexdigest()
    image_count = sum(bool(card.get("imageURL") or card.get("largeImageURL")) for card in cards)
    priced_count = sum(card.get("marketPrice") is not None for card in cards)
    price_option_count = sum(len(card.get("priceOptions") or []) for card in cards)
    languages = sorted({str(card.get("language") or "English") for card in cards})
    manifest = {
        "schemaVersion": 2,
        "catalogVersion": now.strftime("%Y.%m.%d.%H%M"),
        "generatedAt": now.isoformat().replace("+00:00", "Z"),
        "cardCount": len(cards),
        "sha256": digest,
        "minimumAppVersion": "1.0",
        "catalogPath": args.catalog_path,
        "source": "Pokemon TCG Data + Pokemon TCG API V2",
        "identitySource": "PokemonTCG/pokemon-tcg-data",
        "artworkSource": "Pokemon TCG API image URLs",
        "sourceRevision": args.source_revision or now.isoformat().replace("+00:00", "Z"),
        "imageCardCount": image_count,
        "pricedCardCount": priced_count,
        "priceOptionCount": price_option_count,
        "freshPricedCardCount": health["freshPricedCardCount"],
        "carriedForwardPricedCardCount": health["carriedForwardPriceCardCount"],
        "carriedForwardArtworkCardCount": health["carriedForwardArtworkCardCount"],
        "healthStatus": health["status"],
        "healthPath": "health.json",
        "healthWarnings": health["warnings"],
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
