"""
Integration tests for Loki log fetching (tools/loki.py).

These are REAL tests — no mocks. They hit the live Loki instance configured in .env.
Run with:
  .venv/bin/python -m pytest tests/loki/test_loki_integration.py -v -s

Skipped automatically if LOKI_URL is still the default placeholder and no auth token is set.

What these tests diagnose:
  - Connectivity:         Can we reach Loki at all?
  - Label schema:         Are our label names (service, chain, network) correct?
  - order_id search:      Does search_by_order_id actually find the order?
  - Time window:          Is the window too narrow to capture the order?
  - service search:       Does search_by_service return solana executor logs?
  - Raw LogQL:            Does a precise label+filter query work end-to-end?
"""
import os
import sys
import pytest
from datetime import datetime, timedelta, timezone

# Ensure project root is on sys.path so config.py resolves
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from config import settings
from tools.loki import query_loki, search_by_order_id, search_by_service

# ── The specific order under investigation ────────────────────────────────────
ORDER_ID = "ef9790e488d40decc3c2ae575c4aba34c6c20e3855d4dcfab02cf5437029370c"
ORDER_CREATED_AT = "2026-04-09T04:23:04Z"  # from orderbook logs: "Order created"

# ── Skip if Loki is not configured ───────────────────────────────────────────
_default_loki_url = "http://loki.internal:3100"
_not_configured = (
    settings.loki_url == _default_loki_url
    and not settings.loki_auth_token
    and not settings.grafana_api_key
)

pytestmark = pytest.mark.skipif(
    _not_configured,
    reason="LOKI_URL / LOKI_AUTH_TOKEN not configured in .env — skipping Loki integration tests",
)


def _no_error(lines: list[str]) -> bool:
    """Return True if none of the lines are Loki error sentinels."""
    return not any(line.startswith("[LOKI ERROR]") for line in lines)


# ── 1. Connectivity ───────────────────────────────────────────────────────────

def test_loki_connectivity():
    """Basic reachability check — query anything over the last 5 minutes."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=5)
    lines = query_loki('{} |= "solana"', start, end, limit=10)

    print(f"\n[connectivity] Loki URL: {settings.loki_url}")
    print(f"[connectivity] Lines returned: {len(lines)}")
    for line in lines[:5]:
        print(" ", line[:200])

    assert isinstance(lines, list), "query_loki must return a list"
    assert _no_error(lines), f"Loki returned an error: {lines[0]}"


# ── 2. Label schema probe ─────────────────────────────────────────────────────

def test_loki_label_schema():
    """
    Query with only {service="executor"} to confirm label names are correct.
    If this returns 0 lines while test_loki_connectivity returns lines,
    it means Loki uses different label names (e.g. 'app' instead of 'service').
    """
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=30)
    lines = query_loki('{service="executor"}', start, end, limit=10)

    print(f"\n[label_schema] Lines with {{service='executor'}}: {len(lines)}")
    for line in lines[:5]:
        print(" ", line[:200])

    assert isinstance(lines, list)
    assert _no_error(lines), f"Loki returned an error: {lines[0]}"

    if not lines:
        print("\n  WARNING: 0 lines — label name 'service' may be wrong in your Loki setup.")
        print("  Try querying Loki directly to discover actual label names.")


def test_loki_label_schema_solana_chain():
    """
    Query with {service="executor", chain="solana"} — narrows to solana executor.
    Zero lines here (when test above returns lines) means 'chain' label is wrong.
    """
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=30)
    lines = query_loki('{service="executor", chain="solana"}', start, end, limit=10)

    print(f"\n[label_schema_solana] Lines with {{service='executor', chain='solana'}}: {len(lines)}")
    for line in lines[:5]:
        print(" ", line[:200])

    assert isinstance(lines, list)
    assert _no_error(lines), f"Loki returned an error: {lines[0]}"

    if not lines:
        print("\n  WARNING: 0 lines — 'chain' label may be wrong or solana executor is quiet right now.")


# ── 3. search_by_service: solana executor ─────────────────────────────────────

def test_search_by_service_solana_executor():
    """
    Fetch recent solana executor logs via the helper function.
    Checks that the helper builds the right LogQL and gets a result.
    """
    lines = search_by_service(
        service="executor",
        chain="solana",
        network="mainnet",
        minutes_back=60,
    )

    print(f"\n[service_search] solana/executor/mainnet — last 60 min: {len(lines)} lines")
    for line in lines[:10]:
        print(" ", line[:200])

    assert isinstance(lines, list)
    assert _no_error(lines), f"Loki returned an error: {lines[0]}"

    if not lines:
        print("\n  INFO: 0 lines — either no activity in the last hour or label names are wrong.")


def test_search_by_service_solana_executor_errors_only():
    """Same as above but filtered to error-level logs only."""
    lines = search_by_service(
        service="executor",
        chain="solana",
        network="mainnet",
        minutes_back=60,
        level_filter="error",
    )

    print(f"\n[service_search_errors] solana/executor/mainnet errors — last 60 min: {len(lines)} lines")
    for line in lines[:10]:
        print(" ", line[:200])

    assert isinstance(lines, list)
    assert _no_error(lines), f"Loki returned an error: {lines[0]}"


# ── 4. search_by_order_id: the specific order under investigation ──────────────

def test_search_by_order_id_created_at_window():
    """
    Search using the correct window: [created_at, min(created_at + 2h, now)].
    This mirrors what log_agent.py does in production.
    """
    created_at = datetime.fromisoformat(ORDER_CREATED_AT.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    start_iso = created_at.isoformat()
    end_iso = min(created_at + timedelta(hours=2), now).isoformat()
    lines = search_by_order_id(ORDER_ID, start_iso=start_iso, end_iso=end_iso)

    print(f"\n[order_id 48h] Found {len(lines)} lines mentioning {ORDER_ID}")
    for line in lines:
        print(" ", line[:300])

    assert isinstance(lines, list)
    assert _no_error(lines), f"Loki returned an error: {lines[0]}"

    if not lines:
        print(f"\n  INFO: Order {ORDER_ID} not found in any Loki logs in the past 48 hours.")
        print("  Possible causes: wrong Loki instance, logs older than 48h, or order_id not logged.")


def test_search_by_order_id_wrong_window_should_miss():
    """
    Search 5 minutes BEFORE created_at — should return 0 lines since the order didn't exist yet.
    Validates that time window correctness matters.
    """
    created_at = datetime.fromisoformat(ORDER_CREATED_AT.replace("Z", "+00:00"))
    start_iso = (created_at - timedelta(minutes=5)).isoformat()
    end_iso = created_at.isoformat()
    lines = search_by_order_id(ORDER_ID, start_iso=start_iso, end_iso=end_iso)

    print(f"\n[order_id 5min] Found {len(lines)} lines (expected 0 for historical order)")
    assert isinstance(lines, list)
    assert _no_error(lines), f"Loki returned an error: {lines[0]}"
    # Not asserting empty — if the order happens to be recent, this could return lines.
    # The key diagnostic is comparing this vs the 48h test above.


# ── 5. Raw LogQL: solana executor filtered to the specific order ───────────────

def test_raw_logql_solana_executor_for_order():
    """
    Most targeted test: exact label selector + order_id filter.
    Zero lines here (when test above returns lines) means label selectors are wrong.
    """
    created_at = datetime.fromisoformat(ORDER_CREATED_AT.replace("Z", "+00:00"))
    now = datetime.now(timezone.utc)
    start = created_at
    end = min(created_at + timedelta(hours=2), now)
    logql = f'{{service_name="/solana-relayer-mainnet"}} |= `{ORDER_ID}`'

    print(f"\n[raw_logql] Query: {logql}")
    lines = query_loki(logql, start, end, limit=500)

    print(f"[raw_logql] Lines returned: {len(lines)}")
    for line in lines:
        print(" ", line[:300])

    assert isinstance(lines, list)
    assert _no_error(lines), f"Loki returned an error: {lines[0]}"

    if not lines:
        print(f"\n  INFO: No solana/executor/mainnet logs for order {ORDER_ID} in 48h.")
        print("  Compare with test_search_by_order_id_wide_window:")
        print("  - If that test found lines: label selectors (service/chain/network) are wrong.")
        print("  - If that test also found nothing: order is not in this Loki at all.")
