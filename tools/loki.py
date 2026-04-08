"""
Loki HTTP API client and tool definitions for the Log Intelligence Agent.
Uses the Loki query_range endpoint directly for best performance.
"""
import httpx
from datetime import datetime, timezone
from config import settings


def _loki_headers() -> dict:
    headers = {"Content-Type": "application/json"}
    if settings.loki_auth_token:
        headers["Authorization"] = f"Bearer {settings.loki_auth_token}"
    elif settings.grafana_api_key:
        # Grafana basic auth fallback: user is always "api_key"
        import base64
        creds = base64.b64encode(f"api_key:{settings.grafana_api_key}".encode()).decode()
        headers["Authorization"] = f"Basic {creds}"
    return headers


def _loki_base_url() -> str:
    # Prefer direct Loki; fallback to Grafana proxy
    if settings.loki_url:
        return settings.loki_url
    if settings.grafana_url:
        return f"{settings.grafana_url}/api/datasources/proxy/1"
    raise RuntimeError("No Loki or Grafana URL configured")


def _to_ns(dt: datetime) -> str:
    """Convert datetime to nanosecond epoch string for Loki."""
    return str(int(dt.timestamp() * 1e9))


def query_loki(logql: str, start: datetime, end: datetime, limit: int = 200) -> list[str]:
    """
    Run a LogQL query against Loki and return matching log lines.

    Args:
        logql: LogQL query string, e.g. '{service="executor",chain="bitcoin"} |= "error"'
        start: Query start time
        end: Query end time
        limit: Max number of log lines to return (default 200)

    Returns:
        List of log line strings, ordered oldest-first
    """
    url = f"{_loki_base_url()}/loki/api/v1/query_range"
    params = {
        "query": logql,
        "start": _to_ns(start),
        "end": _to_ns(end),
        "limit": limit,
        "direction": "forward",
    }
    try:
        resp = httpx.get(url, params=params, headers=_loki_headers(), timeout=30)
        resp.raise_for_status()
        data = resp.json()
        lines = []
        for stream in data.get("data", {}).get("result", []):
            for _ts, line in stream.get("values", []):
                lines.append(line)
        return lines
    except httpx.HTTPStatusError as e:
        return [f"[LOKI ERROR] HTTP {e.response.status_code}: {e.response.text[:200]}"]
    except Exception as e:
        return [f"[LOKI ERROR] {type(e).__name__}: {e}"]


def search_by_order_id(order_id: str, minutes_back: int = 30) -> list[str]:
    """
    Search all logs for a specific order_id across all services.

    Args:
        order_id: The order ID to search for
        minutes_back: How many minutes of history to search (default 30)

    Returns:
        List of log lines containing the order_id
    """
    from datetime import timedelta
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=minutes_back)
    logql = f'{{}} |= `{order_id}`'
    return query_loki(logql, start, end, limit=500)


def search_by_service(
    service: str,
    chain: str,
    network: str,
    minutes_back: int = 30,
    level_filter: str = "",
) -> list[str]:
    """
    Search logs for a specific service/chain/network combination.

    Args:
        service: Service name (executor, watcher, relayer)
        chain: Chain name (bitcoin, evm, solana)
        network: Network (mainnet, testnet)
        minutes_back: How many minutes of history to search
        level_filter: Optional log level filter, e.g. 'error' or 'warn'

    Returns:
        List of matching log lines
    """
    from datetime import timedelta
    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=minutes_back)

    # Build label selector — adjust these label names to match your Loki setup
    logql = f'{{service="{service}", chain="{chain}", network="{network}"}}'
    if level_filter:
        logql += f' |= `{level_filter}`'

    return query_loki(logql, start, end, limit=300)


# Tool definitions for Claude (raw JSON schema format)
LOKI_TOOL_DEFINITIONS = [
    {
        "name": "query_loki",
        "description": (
            "Run a raw LogQL query against Loki and return matching log lines. "
            "Use this for precise queries with specific label selectors and filters. "
            "Example: '{service=\"executor\", chain=\"bitcoin\"} |= \"error\" | json'"
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
            "Search all Loki logs for a specific order_id. "
            "Returns log lines from all services that mention this order."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {
                    "type": "string",
                    "description": "The order ID to search for",
                },
                "minutes_back": {
                    "type": "integer",
                    "description": "How many minutes of log history to search (default 30)",
                    "default": 30,
                },
            },
            "required": ["order_id"],
        },
    },
    {
        "name": "search_by_service",
        "description": (
            "Search logs for a specific service, chain, and network combination. "
            "Optionally filter by log level (error, warn, info)."
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
                    "enum": ["bitcoin", "evm", "solana"],
                    "description": "The chain to query logs for",
                },
                "network": {
                    "type": "string",
                    "enum": ["mainnet", "testnet"],
                    "description": "The network environment",
                },
                "minutes_back": {
                    "type": "integer",
                    "description": "How many minutes of log history to search (default 30)",
                    "default": 30,
                },
                "level_filter": {
                    "type": "string",
                    "description": "Optional log level keyword to filter by, e.g. 'error' or 'warn'",
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
            tool_input.get("minutes_back", 30),
        )
        return "\n".join(lines) if lines else "[No logs found for this order_id]"

    elif tool_name == "search_by_service":
        lines = search_by_service(
            tool_input["service"],
            tool_input["chain"],
            tool_input["network"],
            tool_input.get("minutes_back", 30),
            tool_input.get("level_filter", ""),
        )
        return "\n".join(lines) if lines else "[No logs found]"

    return f"[Unknown tool: {tool_name}]"
