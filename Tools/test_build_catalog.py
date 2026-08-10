import datetime as dt
import importlib.util
import pathlib
import unittest


MODULE_PATH = pathlib.Path(__file__).with_name("build_catalog.py")
SPEC = importlib.util.spec_from_file_location("build_catalog", MODULE_PATH)
build_catalog = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(build_catalog)


class CatalogResilienceTests(unittest.TestCase):
    def test_preserves_recent_missing_price_and_original_timestamp(self):
        current = [{"id": "sv1-1", "name": "Test", "providerIDs": {"pokemonTCG": "sv1-1"}}]
        previous = [{
            "id": "sv1-1",
            "marketPrice": 4.25,
            "priceSource": "TCGplayer market",
            "priceUpdatedAt": "2026/08/09",
            "priceFinish": "normal",
            "priceOptions": [{"finish": "normal", "market": 4.25}],
        }]

        stats = build_catalog.carry_forward_last_known_good(
            current,
            previous,
            now=dt.datetime(2026, 8, 10, tzinfo=dt.timezone.utc),
            max_price_age_days=90,
        )

        self.assertEqual(current[0]["marketPrice"], 4.25)
        self.assertEqual(current[0]["priceUpdatedAt"], "2026/08/09")
        self.assertEqual(stats["carriedForwardPriceCardCount"], 1)

    def test_does_not_preserve_expired_price(self):
        current = [{"id": "sv1-1"}]
        previous = [{"id": "sv1-1", "marketPrice": 4.25, "priceUpdatedAt": "2025/01/01"}]

        build_catalog.carry_forward_last_known_good(
            current,
            previous,
            now=dt.datetime(2026, 8, 10, tzinfo=dt.timezone.utc),
            max_price_age_days=90,
        )

        self.assertNotIn("marketPrice", current[0])

    def test_current_price_wins_while_missing_finish_is_preserved(self):
        current = [{
            "id": "sv1-1",
            "marketPrice": 5.0,
            "priceUpdatedAt": "2026/08/10",
            "priceOptions": [{"finish": "normal", "market": 5.0}],
        }]
        previous = [{
            "id": "sv1-1",
            "marketPrice": 4.0,
            "priceUpdatedAt": "2026/08/09",
            "priceOptions": [
                {"finish": "normal", "market": 4.0},
                {"finish": "reverseHolofoil", "market": 7.0},
            ],
        }]

        build_catalog.carry_forward_last_known_good(
            current,
            previous,
            now=dt.datetime(2026, 8, 10, tzinfo=dt.timezone.utc),
            max_price_age_days=90,
        )

        self.assertEqual(current[0]["marketPrice"], 5.0)
        self.assertEqual(len(current[0]["priceOptions"]), 2)

    def test_health_gate_rejects_large_card_count_drop(self):
        current = [{"id": str(index)} for index in range(90)]
        previous = [{"id": str(index)} for index in range(100)]
        stats = {
            "carriedForwardPriceCardCount": 0,
            "carriedForwardPriceOptionCount": 0,
            "carriedForwardArtworkCardCount": 0,
        }

        report = build_catalog.build_health_report(
            current,
            previous,
            stats,
            generated_at=dt.datetime(2026, 8, 10, tzinfo=dt.timezone.utc),
        )

        self.assertEqual(report["status"], "failed")
        self.assertTrue(report["errors"])


if __name__ == "__main__":
    unittest.main()
