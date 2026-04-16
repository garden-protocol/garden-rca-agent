# tests/loki/test_loki_routing.py
"""
Unit tests for tools/loki.py routing logic.

These tests MOCK the underlying HTTP layer (`_query`) so they run offline
and do not require a configured Loki instance. They assert:
  - Correct Loki instance (primary vs solver) is selected per service
  - Correct LogQL label selector is built
  - solver_id filter is applied only where appropriate
  - level_filter regex matches real level tokens but not arbitrary substrings
"""
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from tools import loki as loki_mod
from tools.loki import (
    _PRIMARY_SHARED_SERVICES,
    _SOLVER_SHARED_SERVICES,
    search_by_service,
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _call_and_capture(service, chain="evm", network="mainnet", **kwargs):
    """Call search_by_service with _query stubbed; return the (url, logql) captured."""
    captured = {}

    def fake_query(base_url, headers, logql, start, end, limit):
        captured["base_url"] = base_url
        captured["logql"] = logql
        captured["limit"] = limit
        return []

    with patch.object(loki_mod, "_query", side_effect=fake_query):
        search_by_service(service, chain, network, **kwargs)

    return captured


# ── Sentinel test: maps must exist ──────────────────────────────────────────

def test_shared_service_maps_exist():
    """Smoke test: the two new maps must be importable with expected keys."""
    assert "solver-engine" in _SOLVER_SHARED_SERVICES
    assert "solver-comms" in _SOLVER_SHARED_SERVICES
    assert "orderbook" in _PRIMARY_SHARED_SERVICES


# ── Solver-shared service routing ────────────────────────────────────────────

def test_solver_engine_routes_to_solver_loki_with_solver_id():
    """solver-engine + solver_id → solver Loki, LogQL filters by both labels."""
    with patch.object(loki_mod, "_solver_url", return_value="http://solver.loki"):
        with patch.object(loki_mod, "_primary_url", return_value="http://primary.loki"):
            captured = _call_and_capture(
                "solver-engine",
                chain="evm",
                network="mainnet",
                solver_id="s-123",
            )
    assert captured["base_url"] == "http://solver.loki"
    assert 'service_name="solver-engine"' in captured["logql"]
    assert 'solver_id="s-123"' in captured["logql"]


def test_solver_engine_routes_to_solver_loki_without_solver_id():
    """solver-engine with no solver_id → still routes to solver Loki."""
    with patch.object(loki_mod, "_solver_url", return_value="http://solver.loki"):
        with patch.object(loki_mod, "_primary_url", return_value="http://primary.loki"):
            captured = _call_and_capture(
                "solver-engine", chain="evm", network="mainnet",
            )
    assert captured["base_url"] == "http://solver.loki"
    assert 'service_name="solver-engine"' in captured["logql"]
    assert "solver_id" not in captured["logql"]


def test_solver_comms_routes_to_solver_loki():
    """solver-comms behaves the same as solver-engine."""
    with patch.object(loki_mod, "_solver_url", return_value="http://solver.loki"):
        with patch.object(loki_mod, "_primary_url", return_value="http://primary.loki"):
            captured = _call_and_capture(
                "solver-comms",
                chain="evm",
                network="mainnet",
                solver_id="s-999",
            )
    assert captured["base_url"] == "http://solver.loki"
    assert 'service_name="solver-comms"' in captured["logql"]
    assert 'solver_id="s-999"' in captured["logql"]


# ── Primary-shared service routing ───────────────────────────────────────────

def test_orderbook_routes_to_primary_loki():
    """orderbook → primary Loki, uses configured service_name, ignores solver_id."""
    with patch.object(loki_mod, "_solver_url", return_value="http://solver.loki"):
        with patch.object(loki_mod, "_primary_url", return_value="http://primary.loki"):
            captured = _call_and_capture(
                "orderbook",
                chain="evm",
                network="mainnet",
                solver_id="s-123",  # should be ignored
            )
    assert captured["base_url"] == "http://primary.loki"
    assert 'service_name="/orderbook-mainnet"' in captured["logql"]
    assert "solver_id" not in captured["logql"]


# ── Level filter regex ───────────────────────────────────────────────────────

from tools.loki import _level_filter_logql  # introduced in this task


def test_level_filter_produces_regex_filter():
    """level_filter should emit |~ (regex) LogQL, not |= (substring)."""
    frag = _level_filter_logql("error")
    assert frag.strip().startswith("|~"), f"expected regex filter, got: {frag!r}"
    assert "error" in frag


def test_level_filter_matches_json_logs():
    """Simulate the regex fragment against sample JSON log lines."""
    import re
    frag = _level_filter_logql("error")
    pattern = frag.split("`")[1]  # extract the regex between backticks
    compiled = re.compile(pattern)
    assert compiled.search('{"level":"error","msg":"oops"}')
    assert compiled.search('{"msg":"oops","level":"ERROR"}')  # case-insensitive


def test_level_filter_matches_logfmt():
    import re
    frag = _level_filter_logql("warn")
    pattern = frag.split("`")[1]
    compiled = re.compile(pattern)
    assert compiled.search("ts=2026-04-10 level=warn msg=backoff")
    assert compiled.search("level = warn msg=...")


def test_level_filter_matches_bracketed_level():
    import re
    frag = _level_filter_logql("error")
    pattern = frag.split("`")[1]
    compiled = re.compile(pattern)
    assert compiled.search("2026-04-10 [ERROR] connection refused")
    assert compiled.search("ERROR: something failed")


def test_level_filter_does_not_match_substring_error():
    """The whole point: substring 'error' inside words must NOT match."""
    import re
    frag = _level_filter_logql("error")
    pattern = frag.split("`")[1]
    compiled = re.compile(pattern)
    assert not compiled.search("no error occurred today")
    assert not compiled.search("error_count=0 completed ok")
    assert not compiled.search("had an error-free run")


def test_search_by_service_uses_regex_filter_for_level():
    """Integration: search_by_service with level_filter emits |~ in LogQL."""
    with patch.object(loki_mod, "_solver_url", return_value="http://solver.loki"):
        with patch.object(loki_mod, "_primary_url", return_value="http://primary.loki"):
            captured = _call_and_capture(
                "watcher",
                chain="evm",
                network="mainnet",
                level_filter="error",
            )
    assert "|~" in captured["logql"]
    assert "|= `error`" not in captured["logql"]
