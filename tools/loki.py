"""
Loki HTTP API client and tool definitions for the Log Intelligence Agent.

Two Loki instances:
  - Primary (LOKI_URL):        infrastructure logs — relayers, watchers, orderbook, etc.
  - Solver  (LOKI_SOLVER_URL): executor logs — solana-executor, evm-executor, btc-executor, etc.

search_by_order_id queries both and merges results.
search_by_service routes to the correct instance based on service type.
"""
import base64
import httpx
from datetime import datetime, timezone
from config import settings


# ── Auth helpers ──────────────────────────────────────────────────────────────

def _make_headers(token: str) -> dict:
    """Build Authorization header, detecting Basic vs Bearer from token content."""
    headers = {"Content-Type": "application/json"}
    if not token:
        return headers
    try:
        decoded = base64.b64decode(token).decode()
        auth_type = "Basic" if ":" in decoded else "Bearer"
    except Exception:
        auth_type = "Bearer"
    headers["Authorization"] = f"{auth_type} {token}"
    return headers


def _primary_headers() -> dict:
    if settings.loki_auth_token:
        return _make_headers(settings.loki_auth_token)
    if settings.grafana_api_key:
        creds = base64.b64encode(f"api_key:{settings.grafana_api_key}".encode()).decode()
        return {"Content-Type": "application/json", "Authorization": f"Basic {creds}"}
    return {"Content-Type": "application/json"}


def _solver_headers() -> dict:
    return _make_headers(settings.loki_solver_auth_token)


def _primary_url() -> str:
    if settings.loki_url:
        return settings.loki_url
    if settings.grafana_url:
        return f"{settings.grafana_url}/api/datasources/proxy/1"
    raise RuntimeError("No Loki URL configured")


def _solver_url() -> str:
    if not settings.loki_solver_url:
        raise RuntimeError("LOKI_SOLVER_URL not configured")
    return settings.loki_solver_url


# ── Core query ────────────────────────────────────────────────────────────────

def _to_ns(dt: datetime) -> str:
    """Convert datetime to nanosecond epoch string for Loki."""
    return str(int(dt.timestamp() * 1e9))


def _level_filter_logql(level: str) -> str:
    """
    Build a LogQL filter fragment that matches real level tokens across
    common log formats (JSON, logfmt, bracketed, bare prefix).

    Matches:
      "level":"error"         (JSON)
      level=error / level = error   (logfmt)
      [ERROR], ERROR:               (plain-text)
    Does NOT match substrings like "no error" or "error_count=0".
    Case-insensitive.
    """
    regex = (
        f'(?i)(?:"?level"?\\s*[:=]\\s*"?{level}\\b'
        f'|\\[{level}\\]'
        f'|\\b{level}\\b(?=:))'
    )
    return f' |~ `{regex}`'


def _query(base_url: str, headers: dict, logql: str, start: datetime, end: datetime, limit: int) -> list[str]:
    """Low-level LogQL query against a Loki instance."""
    url = f"{base_url}/loki/api/v1/query_range"
    params = {
        "query": logql,
        "start": _to_ns(start),
        "end": _to_ns(end),
        "limit": limit,
        "direction": "forward",
    }
    try:
        resp = httpx.get(url, params=params, headers=headers, timeout=30)
        resp.raise_for_status()
        lines = []
        for stream in resp.json().get("data", {}).get("result", []):
            for _ts, line in stream.get("values", []):
                lines.append(line)
        return lines
    except httpx.HTTPStatusError as e:
        return [f"[LOKI ERROR] HTTP {e.response.status_code}: {e.response.text[:200]}"]
    except Exception as e:
        return [f"[LOKI ERROR] {type(e).__name__}: {e}"]


def query_loki(logql: str, start: datetime, end: datetime, limit: int = 200) -> list[str]:
    """
    Run a raw LogQL query against the primary Loki (infrastructure logs).

    Args:
        logql: LogQL query string
        start: Query start time
        end: Query end time
        limit: Max log lines to return (default 200)

    Returns:
        List of log line strings, ordered oldest-first
    """
    return _query(_primary_url(), _primary_headers(), logql, start, end, limit)


def query_solver_loki(logql: str, start: datetime, end: datetime, limit: int = 200) -> list[str]:
    """
    Run a raw LogQL query against the solver Loki (executor logs).

    Args:
        logql: LogQL query string
        start: Query start time
        end: Query end time
        limit: Max log lines to return (default 200)

    Returns:
        List of log line strings, ordered oldest-first
    """
    return _query(_solver_url(), _solver_headers(), logql, start, end, limit)


# ── Service → container name mappings ─────────────────────────────────────────

# Primary Loki: infrastructure services
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

# Solver Loki: executor services (label is `container`, not `service_name`)
_SOLVER_SERVICE_MAP: dict[str, str] = {
    "solana":   "solana-executor",
    "evm":      "evm-executor",
    "bitcoin":  "btc-executor",
    "litecoin": "litecoin-executor",
    "tron":     "tron-executor",
    "starknet": "starknet-executor",
    "spark":    "spark-executor",
    "alpen":    "alpen-executor",
    "xrpl":     "xrpl-executor",
}

# Services on solver Loki that are NOT chain-scoped. Filtered by solver_id
# when one is provided. `chain` and `network` args are accepted for API
# symmetry but ignored for these services.
_SOLVER_SHARED_SERVICES: dict[str, str] = {
    "solver-engine": "solver-engine",
    "solver-comms":  "solver-comms",
}

# Services on primary Loki that are NOT chain-scoped. `chain`, `network`,
# and `solver_id` args are ignored for these services.
_PRIMARY_SHARED_SERVICES: dict[str, str] = {
    "orderbook": "/orderbook-mainnet",  # also contains quote service logs
}


# ── High-level search functions ───────────────────────────────────────────────

def search_by_order_id(
    order_id: str,
    start_iso: str | None = None,
    end_iso: str | None = None,
    minutes_back: int = 30,
    solver_id: str = "",
) -> list[str]:
    """
    Search ALL logs (both primary and solver Loki) for a specific order_id.

    Args:
        order_id: The order ID to search for
        start_iso: Explicit start time in ISO 8601 format (overrides minutes_back)
        end_iso: Explicit end time in ISO 8601 format (overrides minutes_back)
        minutes_back: Fallback if start_iso/end_iso not provided (default 30)
        solver_id: Optional solver ID from order response — narrows solver Loki query

    Returns:
        Merged list of log lines from both Loki instances containing the order_id
    """
    from datetime import timedelta
    if start_iso and end_iso:
        start = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
        end = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
    else:
        end = datetime.now(timezone.utc)
        start = end - timedelta(minutes=minutes_back)

    # Query primary Loki (infra logs, exclude explorer-api noise)
    primary_lines = _query(
        _primary_url(), _primary_headers(),
        f'{{job="MAINNET_LOGS", container_name!="/explorer-api"}} |= `{order_id}`',
        start, end, limit=500,
    )

    # Query solver Loki (executor logs) if configured
    solver_lines: list[str] = []
    if settings.loki_solver_url:
        if solver_id:
            selector = f'{{solver_id="{solver_id}"}}'
        else:
            selector = '{container=~".+"}'
        solver_lines = _query(
            _solver_url(), _solver_headers(),
            f'{selector} |= `{order_id}`',
            start, end, limit=500,
        )

    return primary_lines + solver_lines


def search_by_service(
    service: str,
    chain: str,
    network: str,
    start_iso: str | None = None,
    end_iso: str | None = None,
    minutes_back: int = 30,
    level_filter: str = "",
    solver_id: str = "",
) -> list[str]:
    """
    Search logs for a specific service/chain/network combination.
    Routes executor queries to solver Loki; all others to primary Loki.

    Args:
        service: Service name (executor, watcher, relayer)
        chain: Chain name (bitcoin, evm, solana, tron, starknet, ...)
        network: Network (mainnet, testnet)
        start_iso: Explicit start time in ISO 8601 format (overrides minutes_back)
        end_iso: Explicit end time in ISO 8601 format (overrides minutes_back)
        minutes_back: Fallback if start_iso/end_iso not provided
        level_filter: Optional log level filter, e.g. 'error' or 'warn'
        solver_id: Optional solver ID from order response — narrows solver Loki query

    Returns:
        List of matching log lines
    """
    from datetime import timedelta
    if start_iso and end_iso:
        start = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
        end = datetime.fromisoformat(end_iso.replace("Z", "+00:00"))
    else:
        end = datetime.now(timezone.utc)
        start = end - timedelta(minutes=minutes_back)

    # ── Shared solver-Loki services (solver-engine, solver-comms) ────────
    if service in _SOLVER_SHARED_SERVICES:
        svc_name = _SOLVER_SHARED_SERVICES[service]
        labels: list[str] = [f'service_name="{svc_name}"']
        if solver_id:
            labels.append(f'solver_id="{solver_id}"')
        logql = "{" + ", ".join(labels) + "}"
        if level_filter:
            logql += _level_filter_logql(level_filter)
        return _query(_solver_url(), _solver_headers(), logql, start, end, limit=300)

    # ── Shared primary-Loki services (orderbook, contains quote logs) ─────
    if service in _PRIMARY_SHARED_SERVICES:
        svc_name = _PRIMARY_SHARED_SERVICES[service]
        logql = f'{{service_name="{svc_name}"}}'
        if level_filter:
            logql += _level_filter_logql(level_filter)
        return _query(_primary_url(), _primary_headers(), logql, start, end, limit=300)

    if service == "executor":
        # Route to solver Loki — use solver_id + service_name when available
        svc_name = _SOLVER_SERVICE_MAP.get(chain)
        labels: list[str] = []
        if solver_id:
            labels.append(f'solver_id="{solver_id}"')
        if svc_name:
            labels.append(f'service_name="{svc_name}"')
        if labels:
            logql = "{" + ", ".join(labels) + "}"
        else:
            logql = f'{{}} |= `{chain}-executor`'
        if level_filter:
            logql += _level_filter_logql(level_filter)
        return _query(_solver_url(), _solver_headers(), logql, start, end, limit=300)

    else:
        # Route to primary Loki
        container = _PRIMARY_SERVICE_MAP.get((service, chain))
        if container:
            logql = f'{{service_name="{container}"}}'
        else:
            logql = f'{{job="MAINNET_LOGS"}}'
            if not level_filter:
                level_filter = f"{chain}-{service}"
        if level_filter:
            logql += _level_filter_logql(level_filter)
        return _query(_primary_url(), _primary_headers(), logql, start, end, limit=300)


# ── Tool definitions for Claude ───────────────────────────────────────────────

LOKI_TOOL_DEFINITIONS = [
    {
        "name": "query_loki",
        "description": (
            "Run a raw LogQL query against the PRIMARY Loki instance (infrastructure logs: "
            "relayers, watchers, orderbook, screener, explorer). "
            "For executor logs use search_by_service with service='executor'. "
            "Example: '{service_name=\"/evm-relayer-mainnet\"} |= \"error\"'"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "logql": {
                    "type": "string",
                    "description": "A valid LogQL query string",
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
            "required": ["logql", "start_iso", "end_iso"],
        },
    },
    {
        "name": "search_by_order_id",
        "description": (
            "Search ALL logs (both infrastructure and executor Loki) for a specific order_id. "
            "Returns merged log lines from every service that mentions this order. "
            "Always use start_iso/end_iso anchored to order created_at for precise results."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "string",
                    "description": "The order ID to search for",
                },
                "start_iso": {
                    "type": "string",
                    "description": "Start time in ISO 8601 format, e.g. '2026-04-06T16:00:00Z'. Overrides minutes_back.",
                },
                "end_iso": {
                    "type": "string",
                    "description": "End time in ISO 8601 format, e.g. '2026-04-06T18:00:00Z'. Overrides minutes_back.",
                },
                "minutes_back": {
                    "type": "integer",
                    "description": "Fallback: minutes of history from now (default 30). Ignored if start_iso/end_iso provided.",
                    "default": 30,
                },
                "solver_id": {
                    "type": "string",
                    "description": "Solver ID from the order response (create_order.solver_id). Narrows solver Loki query to this solver's logs.",
                    "default": "",
                },
            },
            "required": ["order_id"],
        },
    },
    {
        "name": "search_by_service",
        "description": (
            "Search logs for a specific service, chain, and network. "
            "Automatically routes executor queries to the solver Loki instance. "
            "Optionally filter by log level keyword (error, warn, info). "
            "Always use start_iso/end_iso anchored to order created_at."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "service": {
                    "type": "string",
                    "enum": ["executor", "watcher", "relayer"],
                    "description": "The service to query logs for",
                },
                "chain": {
                    "type": "string",
                    "enum": ["bitcoin", "evm", "solana", "tron", "starknet", "spark", "litecoin", "alpen"],
                    "description": "The chain to query logs for",
                },
                "network": {
                    "type": "string",
                    "enum": ["mainnet", "testnet"],
                    "description": "The network environment",
                },
                "start_iso": {
                    "type": "string",
                    "description": "Start time in ISO 8601 format, e.g. '2026-04-06T16:00:00Z'. Overrides minutes_back.",
                },
                "end_iso": {
                    "type": "string",
                    "description": "End time in ISO 8601 format, e.g. '2026-04-06T18:00:00Z'. Overrides minutes_back.",
                },
                "minutes_back": {
                    "type": "integer",
                    "description": "Fallback: minutes of history from now (default 30). Ignored if start_iso/end_iso provided.",
                    "default": 30,
                },
                "level_filter": {
                    "type": "string",
                    "description": "Optional log level keyword to filter by, e.g. 'error' or 'warn'",
                    "default": "",
                },
                "solver_id": {
                    "type": "string",
                    "description": "Solver ID from the order response (create_order.solver_id). Narrows executor queries to this solver's logs.",
                    "default": "",
                },
            },
            "required": ["service", "chain", "network"],
        },
    },
]


def execute_loki_tool(tool_name: str, tool_input: dict) -> str:
    """Execute a Loki tool call and return result as string."""
    if tool_name == "query_loki":
        start = datetime.fromisoformat(tool_input["start_iso"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(tool_input["end_iso"].replace("Z", "+00:00"))
        lines = query_loki(tool_input["logql"], start, end, tool_input.get("limit", 200))
        return "\n".join(lines) if lines else "[No logs found]"

    elif tool_name == "search_by_order_id":
        lines = search_by_order_id(
            tool_input["order_id"],
            start_iso=tool_input.get("start_iso"),
            end_iso=tool_input.get("end_iso"),
            minutes_back=tool_input.get("minutes_back", 30),
            solver_id=tool_input.get("solver_id", ""),
        )
        return "\n".join(lines) if lines else "[No logs found for this order_id]"

    elif tool_name == "search_by_service":
        lines = search_by_service(
            tool_input["service"],
            tool_input["chain"],
            tool_input["network"],
            start_iso=tool_input.get("start_iso"),
            end_iso=tool_input.get("end_iso"),
            minutes_back=tool_input.get("minutes_back", 30),
            level_filter=tool_input.get("level_filter", ""),
            solver_id=tool_input.get("solver_id", ""),
        )
        return "\n".join(lines) if lines else "[No logs found]"

    return f"[Unknown tool: {tool_name}]"
