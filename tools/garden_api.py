"""
Garden Finance API helpers.

Provides cached access to:
  - /v2/chains  — asset registry (token addresses, HTLC addresses, decimals)
  - /v2/schemas — contract ABIs (ERC20, HTLC variants)
"""
import time
import httpx

CHAINS_URL  = "https://api.garden.finance/v2/chains"
SCHEMAS_URL = "https://api.garden.finance/v2/schemas"

_TTL = 300  # 5-minute cache for both endpoints

_chains_cache:  list[dict] | None = None
_chains_ts:     float = 0
_schemas_cache: dict[str, list] | None = None
_schemas_ts:    float = 0


def get_chains() -> list[dict]:
    """Return the full chain/asset registry, cached for 5 minutes."""
    global _chains_cache, _chains_ts
    now = time.monotonic()
    if _chains_cache is not None and now - _chains_ts < _TTL:
        return _chains_cache
    resp = httpx.get(CHAINS_URL, timeout=10)
    resp.raise_for_status()
    _chains_cache = resp.json()["result"]
    _chains_ts = now
    return _chains_cache


def get_schemas() -> dict[str, list]:
    """Return schemas keyed by name (e.g. 'evm:htlc_erc20'), cached for 5 minutes."""
    global _schemas_cache, _schemas_ts
    now = time.monotonic()
    if _schemas_cache is not None and now - _schemas_ts < _TTL:
        return _schemas_cache
    resp = httpx.get(SCHEMAS_URL, timeout=10)
    resp.raise_for_status()
    _schemas_cache = {s["name"]: s["schema"] for s in resp.json()["result"]}
    _schemas_ts = now
    return _schemas_cache


def lookup_asset(asset_id: str) -> dict | None:
    """
    Look up an asset by its Garden ID (e.g. 'ethereum:usdt').

    Returns:
        {
          "token_address": str | None,   # ERC20 contract address (None for native)
          "htlc_address":  str,          # HTLC contract address
          "decimals":      int,
          "is_native":     bool,         # True for citrea:cbtc, botanix:btc, etc.
          "chain_id":      str,          # e.g. "evm:1"
          "htlc_schema":   str,          # e.g. "evm:htlc_erc20" or "evm:htlc"
        }
    or None if the asset_id is not found.
    """
    for chain in get_chains():
        if chain.get("native_asset_id") == asset_id:
            # Native asset — token address is the zero address / not an ERC20
            for asset in chain.get("assets", []):
                if asset["id"] == asset_id:
                    return {
                        "token_address": None,
                        "htlc_address":  asset["htlc"]["address"],
                        "decimals":      asset.get("decimals", 18),
                        "is_native":     True,
                        "chain_id":      chain["id"],
                        "htlc_schema":   asset["htlc"]["schema"],
                    }
        for asset in chain.get("assets", []):
            if asset["id"] == asset_id:
                token = asset.get("token", {})
                return {
                    "token_address": token.get("address"),
                    "htlc_address":  asset["htlc"]["address"],
                    "decimals":      asset.get("decimals", 18),
                    "is_native":     chain.get("native_asset_id") == asset_id,
                    "chain_id":      chain["id"],
                    "htlc_schema":   asset["htlc"]["schema"],
                }
    return None


def get_abi(schema_name: str) -> list:
    """
    Return the ABI for a named schema (e.g. 'evm:htlc_erc20', 'evm:erc20').
    Raises KeyError if not found.
    """
    schemas = get_schemas()
    if schema_name not in schemas:
        raise KeyError(f"Schema '{schema_name}' not found. Available: {list(schemas.keys())}")
    return schemas[schema_name]
