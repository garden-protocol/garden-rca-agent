"""
Integration test: fetch destination executor logs for a specific order.

Order ca0413...c3e9 has:
  - source:      Bitcoin (primary/BTC)
  - destination:  EVM (0xe35d...eC04)
  - created_at:   2025-11-16T01:57:00.236899+00:00

We query the Solver Loki (executor logs) for the EVM executor container
in the window [created_at, created_at + 1h].

Run:
  .venv/bin/python -m pytest tests/loki/test_dest_executor_logs.py -v -s
"""
import os
import sys
import pytest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from config import settings
from tools.loki import query_solver_loki, search_by_order_id, search_by_service

# ── Order under test ────────────────────────────────────────────────────────
ORDER_ID = "ca0413491e9e374fd9f40b07d101da5ff37f7e7b99b2f9beee6725b0ea9fc3e9"
ORDER_CREATED_AT = "2025-11-16T01:57:00.236899+00:00"
DEST_CHAIN = "evm"

CREATED_AT = datetime.fromisoformat(ORDER_CREATED_AT)
WINDOW_START = CREATED_AT
WINDOW_END = CREATED_AT + timedelta(hours=1)

# ── Skip if Loki not configured ─────────────────────────────────────────────
_not_configured = (
    settings.loki_url == "http://loki.internal:3100"
    and not settings.loki_auth_token
    and not settings.grafana_api_key
)
pytestmark = pytest.mark.skipif(
    _not_configured,
    reason="LOKI_URL / LOKI_AUTH_TOKEN not configured — skipping",
)


def _no_error(lines: list[str]) -> bool:
    return not any(line.startswith("[LOKI ERROR]") for line in lines)


# ── 1. search_by_service: EVM executor logs in the order's time window ──────

def test_dest_executor_logs_by_service():
    """
    Use search_by_service to fetch EVM executor logs in [created_at, +1h].
    Routes to solver Loki with {container="evm-executor"}.
    """
    lines = search_by_service(
        service="executor",
        chain=DEST_CHAIN,
        network="mainnet",
        start_iso=WINDOW_START.isoformat(),
        end_iso=WINDOW_END.isoformat(),
    )

    print(f"\n[dest_executor_service] EVM executor logs [{WINDOW_START} → {WINDOW_END}]: {len(lines)} lines")
    for line in lines[:20]:
        print(" ", line[:300])

    assert isinstance(lines, list)
    assert _no_error(lines), f"Loki returned an error: {lines[0]}"


# ── 2. Raw LogQL: EVM executor filtered to this specific order ──────────────

def test_dest_executor_logs_for_order():
    """
    Query solver Loki directly: {container="evm-executor"} |= `<order_id>`.
    This is the most targeted query — only lines mentioning this order.
    """
    logql = f'{{container="evm-executor"}} |= `{ORDER_ID}`'

    print(f"\n[dest_executor_order] Query: {logql}")
    lines = query_solver_loki(logql, WINDOW_START, WINDOW_END, limit=500)

    print(f"[dest_executor_order] Lines: {len(lines)}")
    for line in lines:
        print(" ", line[:300])

    assert isinstance(lines, list)
    assert _no_error(lines), f"Loki returned an error: {lines[0]}"

    if not lines:
        print(f"\n  INFO: No evm-executor logs for order {ORDER_ID} in the 1h window.")
        print("  Possible causes:")
        print("    - EVM executor didn't process this order in that window")
        print("    - Logs older than Loki retention period")
        print("    - Container name 'evm-executor' doesn't match solver Loki labels")


# ── 3. search_by_order_id: cross-instance search in the same window ─────────

def test_order_id_search_in_window():
    """
    Search both Loki instances for the order ID in [created_at, +1h].
    Broader than test 2 — catches logs from relayers/watchers too.
    """
    lines = search_by_order_id(
        ORDER_ID,
        start_iso=WINDOW_START.isoformat(),
        end_iso=WINDOW_END.isoformat(),
    )

    print(f"\n[order_id_search] All logs for {ORDER_ID[:16]}... [{WINDOW_START} → {WINDOW_END}]: {len(lines)} lines")
    for line in lines[:30]:
        print(" ", line[:300])

    assert isinstance(lines, list)
    assert _no_error(lines), f"Loki returned an error: {lines[0]}"

    if not lines:
        print(f"\n  INFO: Order {ORDER_ID[:16]}... not found in any Loki instance in the 1h window.")
        print("  This order is from 2025-11-16 — logs may have aged out of retention.")
