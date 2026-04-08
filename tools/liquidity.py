"""
Liquidity check tool for the investigation pipeline.
Queries the configured liquidity_url for solver-specific balances.
"""
import logging

import httpx

from config import settings


logger = logging.getLogger("rca-agent.liquidity")


def check_solver_liquidity(
    solver_id: str,
    dest_chain: str,
    asset: str,
    required_amount: str,
) -> tuple[bool, str]:
    """
    Check whether a specific solver has enough liquidity on the destination chain.

    The liquidity URL returns all solvers' balances; we filter client-side by solver_id.

    Args:
        solver_id:       Solver wallet address (from create_order.solver_id)
        dest_chain:      API chain name, e.g. "ethereum"
        asset:           Asset/token address on the destination chain
        required_amount: Required amount as a string integer

    Returns:
        (True, "") if solver has sufficient liquidity
        (False, reason_message) if insufficient or solver not found
    """
    if not settings.liquidity_url:
        logger.warning("liquidity_url not configured — skipping liquidity check")
        return True, ""

    try:
        resp = httpx.get(settings.liquidity_url, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.warning("Liquidity check failed (HTTP error): %s", exc)
        return True, ""  # non-fatal: skip check on network errors

    required_int = _parse_amount(required_amount)
    solver_id_lower = solver_id.lower()

    # data may be a list of entries or a dict keyed by solver_id
    entries: list[dict] = _normalize_entries(data)

    # Find entries matching this solver, chain, and asset
    solver_entries = [
        e for e in entries
        if _match_solver(e, solver_id_lower)
        and _match_chain(e, dest_chain)
        and _match_asset(e, asset)
    ]

    if not solver_entries:
        logger.info(
            "Solver %s not found in liquidity response for chain=%s asset=%s",
            solver_id, dest_chain, asset,
        )
        return False, (
            f"Solver {solver_id} has no registered liquidity on {dest_chain} "
            f"for asset {asset}. Please fund the solver."
        )

    # Sum available amounts across matching entries
    available = sum(_parse_amount(str(e.get("available_amount", e.get("balance", "0")))) for e in solver_entries)

    if available < required_int:
        shortage = required_int - available
        return False, (
            f"Please fund {shortage} more of {asset} on {dest_chain} "
            f"for solver {solver_id} (have {available}, need {required_int})"
        )

    return True, ""


# ── helpers ──────────────────────────────────────────────────────────────────

def _normalize_entries(data: object) -> list[dict]:
    """Normalise various response shapes into a flat list of dicts."""
    if isinstance(data, list):
        return [e for e in data if isinstance(e, dict)]
    if isinstance(data, dict):
        # Could be {"result": [...]} or {"solvers": [...]} etc.
        for key in ("result", "solvers", "data", "balances"):
            if isinstance(data.get(key), list):
                return [e for e in data[key] if isinstance(e, dict)]
        # Could be {solver_id: {...}} mapping
        return [{"solver_id": k, **v} for k, v in data.items() if isinstance(v, dict)]
    return []


def _parse_amount(value: str) -> int:
    try:
        return int(value)
    except (ValueError, TypeError):
        return 0


def _match_solver(entry: dict, solver_id_lower: str) -> bool:
    for key in ("solver_id", "solver", "address", "id"):
        val = entry.get(key)
        if val and str(val).lower() == solver_id_lower:
            return True
    return False


def _match_chain(entry: dict, dest_chain: str) -> bool:
    for key in ("chain", "network", "chain_name"):
        val = entry.get(key)
        if val and str(val).lower() == dest_chain.lower():
            return True
    return True  # if no chain field, don't filter by chain


def _match_asset(entry: dict, asset: str) -> bool:
    asset_lower = asset.lower()
    for key in ("asset", "token", "token_address", "contract"):
        val = entry.get(key)
        if val and str(val).lower() == asset_lower:
            return True
    return True  # if no asset field, don't filter by asset
