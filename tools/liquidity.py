"""
Liquidity helpers for the investigation pipeline.
Queries the Garden Finance v2/liquidity endpoint.

API shape:
  {
    "status": "Ok",
    "result": [
      {
        "solver_id": "0x...",
        "liquidity": [
          {
            "asset": "ethereum:usdt",   # "{chain}:{token}"
            "address": "0x...",         # solver's wallet on this chain
            "balance": "123456789",     # raw units (wei / lamports / sats)
            "readable_balance": "...",
            "fiat_value": "..."
          },
          ...
        ]
      },
      ...
    ]
  }
"""
import logging

import httpx

from config import settings


logger = logging.getLogger("rca-agent.liquidity")

# Module-level cache so we only fetch once per investigation.
# Invalidated on each new process start (intentional — data is live).
_CACHE: list[dict] | None = None


def _fetch_solvers() -> list[dict]:
    """Fetch and return the list of solver objects from v2/liquidity. Cached per process."""
    global _CACHE
    if _CACHE is not None:
        return _CACHE

    resp = httpx.get(settings.liquidity_url, timeout=10.0)
    resp.raise_for_status()
    data = resp.json()

    # Handle both {"status": "Ok", "result": [...]} and bare list shapes.
    if isinstance(data, list):
        _CACHE = [s for s in data if isinstance(s, dict)]
    elif isinstance(data, dict):
        inner = data.get("result") or data.get("data") or data.get("solvers") or []
        _CACHE = [s for s in inner if isinstance(s, dict)]
    else:
        _CACHE = []

    return _CACHE


def _find_solver(solver_id: str) -> dict | None:
    """Return the solver object matching solver_id (case-insensitive)."""
    sid = solver_id.lower()
    for solver in _fetch_solvers():
        if str(solver.get("solver_id", "")).lower() == sid:
            return solver
    return None


def get_solver_address(solver_id: str, chain: str) -> str | None:
    """
    Return the solver's wallet address for the given chain by inspecting
    the v2/liquidity response.

    Finds the first liquidity entry where asset starts with "{chain}:" and
    returns its address field.

    Returns None if the liquidity URL is not configured, the solver is not
    found, or the solver has no registered liquidity on that chain.
    """
    if not settings.liquidity_url:
        return None

    try:
        solver = _find_solver(solver_id)
        if solver is None:
            logger.info("Solver %s not found in liquidity response", solver_id)
            return None

        prefix = f"{chain.lower()}:"
        for entry in solver.get("liquidity", []):
            asset_key = str(entry.get("asset", "")).lower()
            if asset_key.startswith(prefix):
                addr = entry.get("address", "")
                if addr:
                    return addr

        logger.info("Solver %s has no liquidity entries for chain %s", solver_id, chain)
        return None

    except Exception as exc:
        logger.warning("get_solver_address failed for solver=%s chain=%s: %s", solver_id, chain, exc)
        return None


def check_solver_liquidity(
    solver_id: str,
    dest_chain: str,
    asset: str,
    required_amount: str,
) -> tuple[bool, str]:
    """
    Check whether a specific solver has enough liquidity on the destination chain.

    Args:
        solver_id:       Solver ID (from create_order.solver_id)
        dest_chain:      API chain name, e.g. "ethereum"
        asset:           Asset token name, e.g. "usdt" (without chain prefix)
        required_amount: Required amount as a raw string integer

    Returns:
        (True, "") if solver has sufficient liquidity or check is skipped
        (False, reason_message) if insufficient or solver not found
    """
    if not settings.liquidity_url:
        logger.warning("liquidity_url not configured — skipping liquidity check")
        return True, ""

    try:
        solver = _find_solver(solver_id)
    except Exception as exc:
        logger.warning("Liquidity check failed (HTTP error): %s", exc)
        return True, ""  # non-fatal: skip check on network errors

    if solver is None:
        logger.info(
            "Solver %s not found in liquidity response for chain=%s asset=%s",
            solver_id, dest_chain, asset,
        )
        return False, (
            f"Solver {solver_id} has no registered liquidity on {dest_chain} "
            f"for asset {asset}. Please fund the solver."
        )

    # Match entries by "{dest_chain}:{asset}"
    target_asset = f"{dest_chain.lower()}:{asset.lower()}"
    matching = [
        e for e in solver.get("liquidity", [])
        if str(e.get("asset", "")).lower() == target_asset
    ]

    if not matching:
        return False, (
            f"Solver {solver_id} has no registered liquidity on {dest_chain} "
            f"for asset {asset}. Please fund the solver."
        )

    required_int = _parse_amount(required_amount)
    available = sum(_parse_amount(str(e.get("balance", "0"))) for e in matching)

    if available < required_int:
        shortage = required_int - available
        readable = next((e.get("readable_balance", "") for e in matching), "")
        return False, (
            f"Solver {solver_id} has insufficient liquidity on {dest_chain} "
            f"for {asset}: available={readable or available}, need {required_int}, "
            f"short by {shortage}. Please fund the solver."
        )

    return True, ""


def _parse_amount(value: str) -> int:
    try:
        return int(value)
    except (ValueError, TypeError):
        return 0
