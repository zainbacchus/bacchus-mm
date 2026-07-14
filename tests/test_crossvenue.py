from decimal import Decimal

from bacchus_mm.crossvenue import VenuePair
from bacchus_mm.eventlog import EventLog
from bacchus_mm.exchange.polymarket import parse_book_top, parse_market


def test_parse_market_decodes_json_string_fields():
    m = parse_market(
        {
            "slug": "test-market",
            "question": "Will it rain?",
            "outcomes": '["Yes", "No"]',
            "clobTokenIds": '["111", "222"]',
            "orderPriceMinTickSize": 0.001,
            "volume24hr": 1234.5,
            "endDate": "2026-08-01T00:00:00Z",
        }
    )
    assert m.outcomes == ["Yes", "No"]
    assert m.token_for("yes") == "111"
    assert m.token_for("No") == "222"
    assert m.tick_size == Decimal("0.001")


def test_parse_book_top_picks_best_levels():
    top = parse_book_top(
        {
            "asset_id": "111",
            "timestamp": "1784049007444",
            "bids": [{"price": "0.55", "size": "10"}, {"price": "0.57", "size": "5"}],
            "asks": [{"price": "0.60", "size": "7"}, {"price": "0.58", "size": "3"}],
        }
    )
    assert top.bid == Decimal("0.57")
    assert top.ask == Decimal("0.58")
    assert top.mid == Decimal("0.575")


def test_parse_book_top_empty_book():
    top = parse_book_top({"asset_id": "111", "bids": [], "asks": []})
    assert top.bid is None and top.ask is None and top.mid is None


def test_venue_pair_from_config_defaults():
    p = VenuePair.from_config({"kalshi": "KX-1", "polymarket_slug": "slug-1"})
    assert p.polymarket_outcome is None and p.invert is False
    p2 = VenuePair.from_config(
        {"kalshi": "KX-1", "polymarket_slug": "slug-1", "invert": True, "polymarket_outcome": "No"}
    )
    assert p2.invert is True and p2.polymarket_outcome == "No"


def test_venue_mark_roundtrip_and_divergence_report(tmp_path, capsys):
    log = EventLog(tmp_path, "xv-test")
    log.record_venue_mark(
        "KX-1", "slug-1",
        Decimal("0.44"), Decimal("0.46"),  # kalshi mid 0.45
        Decimal("0.49"), Decimal("0.51"),  # pm mid 0.50
        Decimal("0.05"),
    )
    log.record_venue_mark(
        "KX-1", "slug-1",
        Decimal("0.45"), Decimal("0.47"), Decimal("0.46"), Decimal("0.48"), Decimal("0.01"),
    )
    log.close()

    from bacchus_mm.analyze import run_report

    run_report(tmp_path, "divergence", hours=1)
    out = capsys.readouterr().out
    assert "KX-1" in out
    assert "50.0%" in out  # one of two samples >= 5c
