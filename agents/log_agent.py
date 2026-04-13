"""
Log Intelligence Agent.
Queries Loki for relevant log lines given an alert context,
then returns a structured log summary for use by chain specialists.
"""
from datetime import datetime, timedelta, timezone
from tools.loki import LOKI_TOOL_DEFINITIONS, execute_loki_tool
from models.alert import Alert
from providers import get_provider

from config import settings as _settings

SYSTEM_PROMPT = """\
You are a Log Intelligence Agent for Garden, a cross-chain bridge system.
Your job is to query Loki logs and extract a clear, structured picture of what happened.

You have three tools:
- query_loki: run raw LogQL queries (use for precise filtering)
- search_by_order_id: find all log activity for a specific order
- search_by_service: find all logs for a service/chain/network

CRITICAL: Always use the start_iso and end_iso parameters provided in the alert details \
for ALL log queries. Never rely on minutes_back — the time window is anchored to the order \
creation timestamp, not the current time.

When a solver_id is provided, ALWAYS pass it to search_by_order_id and search_by_service \
calls. This filters executor logs to the specific solver that handled the order.

## Output Format

Your output MUST end with a ```json block containing structured evidence:

```json
{
  "timeline": "chronological sequence of key events",
  "key_evidence": [
    {"line": "exact log line", "significance": "why this matters", "source": "service_name"},
    ...
  ],
  "errors_found": true/false,
  "summary": "2-3 sentence summary of what logs reveal"
}
```

Before the JSON block, write a brief markdown analysis.

## Log Noise to IGNORE (never include these as evidence)
- SQL slow execution warnings ("slow sql", "execution time exceeded", "slow query")
- Routine periodic health checks, heartbeat pings, keepalive messages
- gRPC keepalive pings, HTTP health probes
- Standard startup/shutdown messages (unless they coincide with the incident window)
- Generic "retrying" messages unless they show escalating failure counts
- Prometheus metric scrape logs
- Routine cache expiry/refresh logs unrelated to the order

## What IS Important Evidence
- Error messages mentioning the specific order ID
- Transaction failures (revert, nonce errors, gas errors, timeout)
- State transition failures (mapper → NoOp, failed initiate/redeem/refund)
- Service connectivity issues (executor unreachable, RPC timeout, DB connection error)
- Order processing decisions (why an order was skipped, filtered, or rejected)
- Unexpected state (order not found, duplicate order, mismatched amounts)

Be precise. Quote actual log lines as evidence. Do not speculate beyond what the logs show.
If log queries fail or return nothing, say so clearly — absence of logs IS useful evidence.
"""


def run(alert: Alert) -> dict:
    """
    Query Loki for logs relevant to the given alert.

    Args:
        alert: The incoming alert object

    Returns:
        dict with 'summary' (str markdown report) and 'raw_lines' (list[str])
    """
    # Compute the log query time window: order_created_at ± 1hr
    order_created_at_str = (alert.metadata or {}).get("order_created_at")
    if order_created_at_str:
        order_created_at = datetime.fromisoformat(
            order_created_at_str.replace("Z", "+00:00")
        )
    else:
        # Fallback to alert timestamp if order_created_at not available
        order_created_at = alert.timestamp

    now = datetime.now(timezone.utc)
    window_start = order_created_at.isoformat()
    window_end = min(order_created_at + timedelta(hours=1), now).isoformat()

    solver_id = (alert.metadata or {}).get("solver_id", "")

    alert_context = (
        f"Order ID: {alert.order_id}\n"
        f"Alert type: {alert.alert_type}\n"
        f"Chain: {alert.chain}\n"
        f"Service: {alert.service}\n"
        f"Network: {alert.network}\n"
        f"Alert message: {alert.message}\n"
        f"Timestamp: {alert.timestamp.isoformat()}\n"
        f"Order created at: {order_created_at.isoformat()}\n"
        f"Solver ID: {solver_id}\n"
    )
    if alert.deadline:
        alert_context += f"Deadline: {alert.deadline.isoformat()}\n"
    if alert.metadata:
        alert_context += f"Metadata: {alert.metadata}\n"

    user_message = (
        f"Investigate the following alert and query Loki to find relevant logs.\n\n"
        f"Alert details:\n{alert_context}\n\n"
        f"**IMPORTANT — Time window and solver_id for all queries:**\n"
        f"Use start_iso=\"{window_start}\" and end_iso=\"{window_end}\" "
        f"(order_created_at to created_at + 1 hour) for ALL log queries. "
        f"Do NOT use minutes_back — always pass explicit start_iso/end_iso.\n"
        f"{'Always pass solver_id=\"' + solver_id + '\" to narrow executor log queries.' if solver_id else 'No solver_id available for this order.'}\n\n"
        f"**Query strategy:**\n"
        f"1. Search for the order_id across all services using search_by_order_id "
        f"with the time window above.\n"
        f"2. Query the **{alert.service}** service logs on **{alert.chain}** / **{alert.network}** "
        f"using search_by_service with the same time window. "
        f"Filter for errors and warnings.\n"
        f"3. Look for errors, warnings, and anomalies.\n"
        f"4. Return a structured markdown report of your findings."
    )

    provider = get_provider()
    model = _settings.get_fast_model()
    messages = [{"role": "user", "content": user_message}]
    all_log_lines: list[str] = []
    total_input = total_output = total_cache_read = total_cache_write = 0

    # Agentic loop — capped to prevent runaway cost
    for _turn in range(5):
        response = provider.create_message(
            model=model,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=LOKI_TOOL_DEFINITIONS,
            messages=messages,
        )

        u = response.usage
        total_input       += u.input_tokens
        total_output      += u.output_tokens
        total_cache_read  += u.cache_read_tokens
        total_cache_write += u.cache_creation_tokens

        if response.stop_reason == "end_turn":
            break

        if not response.tool_calls:
            break

        messages.append(provider.build_assistant_message(response))

        tool_results = []
        for tc in response.tool_calls:
            result = execute_loki_tool(tc.name, tc.input)
            # Collect raw lines for upstream use
            if result and not result.startswith("["):
                all_log_lines.extend(result.splitlines())
            tool_results.append((tc.id, result))

        tr_msg = provider.build_tool_results_message(tool_results)
        if isinstance(tr_msg, list):
            messages.extend(tr_msg)
        else:
            messages.append(tr_msg)

    raw_text = response.text or "[Log agent returned no summary]"

    # Extract structured evidence from the trailing JSON block
    key_evidence = []
    summary = raw_text
    import json
    import re
    json_matches = re.findall(r"```json\s*(.*?)\s*```", raw_text, re.DOTALL)
    if json_matches:
        try:
            structured = json.loads(json_matches[-1])
            key_evidence = structured.get("key_evidence", [])
            # Use the LLM's summary if available, otherwise use the full text
            if structured.get("summary"):
                summary_line = structured["summary"]
                # Keep the markdown analysis (everything before the JSON block) + the LLM summary
                json_start = raw_text.rfind("```json")
                summary = raw_text[:json_start].strip() if json_start > 0 else summary_line
        except (json.JSONDecodeError, TypeError):
            pass

    return {
        "summary": summary,
        "key_evidence": key_evidence,
        "raw_lines": all_log_lines[:500],  # kept for debugging, not shown to user
        "usage": {
            "model": model,
            "input_tokens": total_input,
            "output_tokens": total_output,
            "cache_read_tokens": total_cache_read,
            "cache_write_tokens": total_cache_write,
        },
    }
