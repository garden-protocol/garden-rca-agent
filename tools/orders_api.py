"""
Garden Finance Order API client.
Fetches full order data and classifies swap state for the investigation pipeline.
"""
import re
import logging
from datetime import datetime, timezone

import httpx

from config import settings
from models.order import OrderApiResponse, OrderResult
from models.investigate import SwapState


logger = logging.getLogger("rca-agent.orders_api")

# Maps API chain names → internal chain names used by specialists/onchain agents.
# Source of truth: https://api.garden.finance/v2/chains
# EVM chains: all entries whose id starts with "evm:"
# Absent chains (litecoin, starknet, tron, spark) produce "Unsupported chain" early return.
CHAIN_MAP: dict[str, str] = {
    # Bitcoin
    "bitcoin":  "bitcoin",
    # EVM
    "ethereum": "evm",
    "arbitrum": "evm",
    "base":     "evm",
    "citrea":   "evm",
    "botanix":  "evm",
    "bnbchain": "evm",
    "monad":    "evm",
    "hyperevm": "evm",
    "megaeth":  "evm",
    # Solana
    "solana":   "solana",
    # Tron
    "tron":     "tron",
    # Starknet
    "starknet": "starknet",
    # Litecoin
    "litecoin": "litecoin",
    # Alpen
    "alpen":    "alpen",
}

_ORDER_ID_RE = re.compile(r"[0-9a-fA-F]{64}")


def parse_order_id(raw: str) -> str:
    """
    Extract a bare 64-hex-char order ID from either a raw ID or a full URL.

    Examples:
      "7ac235...c78"                           → "7ac235...c78"
      "https://api.garden.finance/v2/orders/7ac235...c78" → "7ac235...c78"
    """
    raw = raw.strip().rstrip("/")
    match = _ORDER_ID_RE.search(raw)
    if match:
        return match.group(0)
    # Fallback: last path segment
    return raw.split("/")[-1]


def normalize_chain(api_chain: str) -> str:
    """Convert API chain name (e.g. 'ethereum') to internal name (e.g. 'evm')."""
    return CHAIN_MAP.get(api_chain.lower(), api_chain.lower())


def fetch_order(order_id: str) -> OrderApiResponse:
    """
    Fetch full order data from Garden Finance API.

    GET {order_api_base_url}/orders/id/{order_id}

    Raises:
        httpx.HTTPStatusError: on non-2xx response
        ValueError: if the response cannot be parsed
    """
    url = f"{settings.order_api_base_url}/orders/id/{order_id}"
    logger.info("Fetching order %s from %s", order_id, url)

    resp = httpx.get(url, timeout=settings.order_api_timeout_seconds)
    resp.raise_for_status()

    try:
        return OrderApiResponse.model_validate(resp.json())
    except Exception as exc:
        raise ValueError(f"Failed to parse order API response: {exc}") from exc


def classify_state(order: OrderApiResponse) -> SwapState:
    """
    Classify the swap into one of the 3 investigation states based on tx hashes.

    Classification rules (checked in order):
      - No source init → Unknown (caller should handle "No User Init" early return)
      - Source inited, zero dest activity → DestInitPending
      - Dest inited but not redeemed → UserRedeemPending
      - Dest redeemed, source not yet redeemed → SolverRedeemPending
      - Otherwise → Unknown (likely already completed or refunded)
    """
    src = order.result.source_swap
    dst = order.result.destination_swap

    if not src.is_initiated:
        return SwapState.UNKNOWN  # "No User Init" handled as early return

    if not dst.is_initiated and not dst.is_redeemed and not dst.is_refunded:
        return SwapState.DEST_INIT_PENDING

    if dst.is_initiated and not dst.is_redeemed and not dst.is_refunded:
        return SwapState.USER_REDEEM_PENDING

    if dst.is_redeemed and not src.is_redeemed and not src.is_refunded:
        return SwapState.SOLVER_REDEEM_PENDING

    return SwapState.UNKNOWN


def fetch_fiat_prices() -> dict[str, float]:
    """
    Fetch current token prices in USD from the Garden Finance fiat endpoint.

    GET https://api.garden.finance/v2/fiat
    Returns a flat dict of {asset_name: usd_price}.
    """
    url = "https://api.garden.finance/v2/fiat"
    resp = httpx.get(url, timeout=settings.order_api_timeout_seconds)
    resp.raise_for_status()
    return resp.json()["result"]  # {"chain:asset": usd_price, ...}


def fetch_order_created_at(order_id: str) -> tuple[datetime, str]:
    """
    Compatibility shim for orchestrator.run() — returns (created_at, source_path).
    Fetches the full order and extracts the created_at timestamp.
    """
    try:
        order = fetch_order(order_id)
        created_at = order.result.created_at
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        return created_at.astimezone(timezone.utc), "api/orders/id"
    except Exception as exc:
        raise RuntimeError(f"Could not fetch order created_at for {order_id}: {exc}") from exc
