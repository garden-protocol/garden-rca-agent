"""Unit tests for blacklist enrichment — offline, no network."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import tools.orders_api as orders_api
from tools.orders_api import _parse_blacklist_map, fetch_blacklist_info
from models.order import BlacklistDetails


# Trimmed real-shape payload from /analytics/metrics/blacklisted-stats.
_PAYLOAD = {
    "status": "Ok",
    "result": {
        "blocked_count": 2,
        "fulfilled_count": 0,
        "blocked_orders": [
            {
                "order_id": "ff878396f7a7b325afec847dab62ee4b0b4961acb926c492b088045c81da6172",
                "blacklisted_address": "bc1py6tud4wyhqdwznmshd24gdre955m6zur6dx04p6pc7t4uxmqj72shlghfz",
                "blacklisted_details": {
                    "address": "bc1py6tud4wyhqdwznmshd24gdre955m6zur6dx04p6pc7t4uxmqj72shlghfz",
                    "chain": "bitcoin",
                    "tag": "",
                    "remarks": "",
                    "flagged_by": "TRM",
                    "blacklisted_at": "2026-06-07T22:29:21.331931Z",
                },
            },
            {
                "order_id": "42b48048e8e31b1de5a731a44e085a2cabc245a8e80fece98bab604d67b692e9",
                "blacklisted_address": "bc1quq29mutxkgxmjfdr7ayj3zd9ad0ld5mrhh89l2",
                "blacklisted_details": {
                    "address": "bc1quq29mutxkgxmjfdr7ayj3zd9ad0ld5mrhh89l2",
                    "chain": "bitcoin",
                    "tag": None,
                    "remarks": None,
                    "flagged_by": "TRM",
                    "blacklisted_at": "2025-06-06T10:00:22.720937Z",
                },
            },
        ],
    },
}


def test_parse_blacklist_map_indexes_by_order_id():
    m = _parse_blacklist_map(_PAYLOAD)
    assert set(m.keys()) == {
        "ff878396f7a7b325afec847dab62ee4b0b4961acb926c492b088045c81da6172",
        "42b48048e8e31b1de5a731a44e085a2cabc245a8e80fece98bab604d67b692e9",
    }
    d = m["ff878396f7a7b325afec847dab62ee4b0b4961acb926c492b088045c81da6172"]
    assert d["flagged_by"] == "TRM"
    assert d["chain"] == "bitcoin"


def test_parse_handles_null_tag_and_remarks():
    m = _parse_blacklist_map(_PAYLOAD)
    d = m["42b48048e8e31b1de5a731a44e085a2cabc245a8e80fece98bab604d67b692e9"]
    # Validates cleanly into the model even with null tag/remarks.
    bd = BlacklistDetails.model_validate(d)
    assert bd.tag is None and bd.remarks is None
    assert bd.flagged_by == "TRM"
    assert bd.blacklisted_at is not None


def test_parse_falls_back_to_flat_address_when_details_missing():
    payload = {"result": {"blocked_orders": [
        {"order_id": "abc", "blacklisted_address": "bc1qfoo"},
    ]}}
    m = _parse_blacklist_map(payload)
    assert m["abc"] == {"address": "bc1qfoo"}


def test_parse_empty_payload_is_safe():
    assert _parse_blacklist_map({}) == {}
    assert _parse_blacklist_map({"result": {}}) == {}
    assert _parse_blacklist_map({"result": {"blocked_orders": None}}) == {}


def test_fetch_blacklist_info_uses_cache(monkeypatch):
    # Seed the module cache directly and confirm lookup hits it without network.
    orders_api._blacklist_cache = _parse_blacklist_map(_PAYLOAD)
    orders_api._blacklist_cache_ts = orders_api.time.monotonic()
    try:
        hit = fetch_blacklist_info(
            "ff878396f7a7b325afec847dab62ee4b0b4961acb926c492b088045c81da6172"
        )
        assert hit is not None and hit["flagged_by"] == "TRM"
        assert fetch_blacklist_info("not-a-real-order") is None
    finally:
        orders_api._blacklist_cache = None
        orders_api._blacklist_cache_ts = 0.0
