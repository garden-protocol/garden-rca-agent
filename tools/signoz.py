"""
SigNoz HTTP API client for primary infrastructure logs (mainnet services).

Replaces the primary Loki instance (LOKI_URL) for:
  - relayers, watchers, orderbook, and other mainnet infrastructure containers

Solver/executor logs remain on Loki (LOKI_SOLVER_URL) — see tools/loki.py.

Uses SigNoz v3 query_range API (POST /api/v3/query_range).
"""
import httpx
from datetime import datetime
from config import settings


# ── Auth & helpers ────────────────────────────────────────────────────────────

def _signoz_headers() -> dict:
    return {
        "Content-Type": "application/json",
        "SIGNOZ-API-KEY": settings.signoz_api_key,
    }


def _to_ns(dt: datetime) -> int:
    return int(dt.timestamp() * 1e9)


# ── Service → container name mappings ─────────────────────────────────────────

# Container names as they appear in SigNoz attributes_string.container_name (with leading slash)
_PRIMARY_SERVICE_MAP: dict[tuple[str, str], str] = {
    ("relayer", "evm"):      "/evm-relayer-mainnet",
    ("watcher", "evm"):      "/evm-watcher-mainnet",
    ("relayer", "solana"):   "/solana-relayer-mainnet",
    ("watcher", "solana"):   "/solana-watcher-mainnet",
    ("watcher", "bitcoin"):  "/bitcoin-indexer-v2",
    ("relayer", "tron"):     "/tron-relayer-mainnet",
    ("watcher", "tron"):     "/tron-watcher",
    ("watcher", "starknet"): "/starknet-watcher-mainnet",
    ("relayer", "starknet"): "/starknet-relayer-mainnet",
    ("watcher", "spark"):    "/spark-watcher-mainnet",
    ("watcher", "litecoin"): "/litecoin-services-mainnet",
    ("watcher", "alpen"):    "/alpen-watcher-mainnet",
}

_PRIMARY_SHARED_SERVICES: dict[str, str] = {
    "orderbook": "/orderbook-mainnet",  # also contains quote service logs
}


# ── Filter helpers ────────────────────────────────────────────────────────────

def _container_filter(containers: list[str]) -> dict:
    return {
        "key": {"key": "container_name", "dataType": "string", "type": "tag", "isColumn": False},
        "op": "in",
        "value": containers,
    }


def _container_not_in_filter(containers: list[str]) -> dict:
    return {
        "key": {"key": "container_name", "dataType": "string", "type": "tag", "isColumn": False},
        "op": "nin",
        "value": containers,
    }


def _body_contains_filter(text: str) -> dict:
    return {
        "key": {"key": "body", "dataType": "string", "type": "", "isColumn": True},
        "op": "contains",
        "value": text,
    }


# ── Core query ────────────────────────────────────────────────────────────────

def _query(filters: list[dict], start: datetime, end: datetime, limit: int) -> list[str]:
    """Low-level SigNoz v3 query_range call."""
    payload = {
        "start": _to_ns(start),
        "end": _to_ns(end),
        "step": 60,
        "variables": {},
        "compositeQuery": {
            "queryType": "builder",
            "panelType": "list",
            "builderQueries": {
                "A": {
                    "dataSource": "logs",
                    "queryName": "A",
                    "expression": "A",
                    "aggregateOperator": "noop",
                    "aggregateAttribute": {"key": "", "dataType": "string", "type": "", "isColumn": False},
                    "filters": {"items": filters, "op": "AND"},
                    "limit": limit,
                    "orderBy": [{"columnName": "timestamp", "order": "asc"}],
                    "pageSize": limit,
                }
            },
        },
    }
    try:
        resp = httpx.post(
            f"{settings.signoz_url}/api/v3/query_range",
            json=payload,
            headers=_signoz_headers(),
            timeout=30,
        )
        resp.raise_for_status()
        result = resp.json().get("data", {}).get("result", [])
        if not result:
            return []
        return [entry["data"]["body"] for entry in result[0].get("list", []) if "data" in entry]
    except httpx.HTTPStatusError as e:
        return [f"[SIGNOZ ERROR] HTTP {e.response.status_code}: {e.response.text[:200]}"]
    except Exception as e:
        return [f"[SIGNOZ ERROR] {type(e).__name__}: {e}"]


# ── Public query functions ────────────────────────────────────────────────────

def query_signoz(q: str, start: datetime, end: datetime, limit: int = 200) -> list[str]:
    """
    Run a SigNoz query against primary infrastructure logs.

    q is parsed as a simple filter string supporting two clauses joined by AND:
      - container_name IN ('name1','name2')
      - container_name NOT IN ('name1')
      - body contains 'keyword'

    Multiple clauses are supported, e.g.:
      "container_name IN ('/evm-watcher-mainnet') AND body contains 'error'"

    Args:
        q: Filter query string (see above)
        start: Query start time
        end: Query end time
        limit: Max log lines to return (default 200)

    Returns:
        List of log body strings, ordered oldest-first
    """
    filters = _parse_query(q)
    return _query(filters, start, end, limit)


def _parse_query(q: str) -> list[dict]:
    """Parse a simple filter query string into SigNoz filter items."""
    import re
    filters = []
    # container_name IN ('a','b') or container_name IN ("a")
    m = re.search(r"container_name\s+IN\s*\(([^)]+)\)", q, re.IGNORECASE)
    if m:
        values = [v.strip().strip("'\"") for v in m.group(1).split(",")]
        filters.append(_container_filter(values))

    # container_name NOT IN ('a')
    m = re.search(r"container_name\s+NOT\s+IN\s*\(([^)]+)\)", q, re.IGNORECASE)
    if m:
        values = [v.strip().strip("'\"") for v in m.group(1).split(",")]
        filters.append(_container_not_in_filter(values))

    # body contains 'keyword'
    m = re.search(r"body\s+contains\s+['\"]([^'\"]+)['\"]", q, re.IGNORECASE)
    if m:
        filters.append(_body_contains_filter(m.group(1)))

    return filters


def search_primary_by_order_id(order_id: str, start: datetime, end: datetime) -> list[str]:
    """Search primary infrastructure logs for a specific order_id."""
    filters = [
        _container_not_in_filter(["/explorer-api"]),
        _body_contains_filter(order_id),
    ]
    return _query(filters, start, end, limit=500)


def search_primary_by_service(
    service: str,
    chain: str,
    start: datetime,
    end: datetime,
    level_filter: str = "",
) -> list[str]:
    """Search primary infrastructure logs for a specific service/chain."""
    filters: list[dict] = []

    if service in _PRIMARY_SHARED_SERVICES:
        container = _PRIMARY_SHARED_SERVICES[service]
        filters.append(_container_filter([container]))
    else:
        container = _PRIMARY_SERVICE_MAP.get((service, chain))
        if container:
            filters.append(_container_filter([container]))
        else:
            filters.append(_body_contains_filter(f"{chain}-{service}"))

    if level_filter:
        filters.append(_body_contains_filter(level_filter))

    return _query(filters, start, end, limit=300)


# ── Tool definition for Claude ────────────────────────────────────────────────

SIGNOZ_TOOL_DEFINITION = {
    "name": "query_signoz",
    "description": (
        "Run a filter query against the PRIMARY infrastructure logs in SigNoz "
        "(relayers, watchers, orderbook). "
        "For executor logs use search_by_service with service='executor'. "
        "Supported filter syntax (clauses joined with AND):\n"
        "  container_name IN ('/evm-relayer-mainnet')\n"
        "  container_name NOT IN ('/explorer-api')\n"
        "  body contains 'keyword'\n"
        "Example: \"container_name IN ('/evm-relayer-mainnet') AND body contains 'error'\""
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "q": {
                "type": "string",
                "description": (
                    "SigNoz filter query. Supported clauses joined with AND:\n"
                    "  container_name IN ('/name') — filter by container\n"
                    "  body contains 'text' — filter by log content\n"
                    "Container names use leading slash, e.g. '/evm-watcher-mainnet', '/orderbook-mainnet'."
                ),
            },
            "start_iso": {
                "type": "string",
                "description": "Start time in ISO 8601 format, e.g. '2026-04-06T17:00:00Z'",
            },
            "end_iso": {
                "type": "string",
                "description": "End time in ISO 8601 format, e.g. '2026-04-06T17:30:00Z'",
            },
            "limit": {
                "type": "integer",
                "description": "Max log lines to return (default 200, max 1000)",
                "default": 200,
            },
        },
        "required": ["q", "start_iso", "end_iso"],
    },
}
