"""
Log Intelligence Agent.
Queries Loki for relevant log lines given an alert context,
then returns a structured log summary for use by chain specialists.
"""
import anthropic
from datetime import datetime, timedelta, timezone
from tools.loki import LOKI_TOOL_DEFINITIONS, execute_loki_tool
from models.alert import Alert


MODEL = "claude-haiku-4-5-20251001"

from config import settings as _settings
client = anthropic.Anthropic(api_key=_settings.anthropic_api_key)

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

Your output should be a structured markdown report with:
1. **Timeline** — chronological sequence of key events from the logs
2. **Errors** — list of error messages found, with context
3. **Warnings** — notable warnings that may indicate upstream issues
4. **Key observations** — patterns, anomalies, repeated failures

Be precise. Quote actual log lines where they are evidence. Do not speculate beyond what the logs show.
If log queries fail or return nothing, say so clearly.
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

    messages = [{"role": "user", "content": user_message}]
    all_log_lines: list[str] = []
    total_input = total_output = total_cache_read = total_cache_write = 0

    # Agentic loop — capped to prevent runaway cost
    for _turn in range(5):
        response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=LOKI_TOOL_DEFINITIONS,
            messages=messages,
        )

        u = response.usage
        total_input       += u.input_tokens
        total_output      += u.output_tokens
        total_cache_read  += getattr(u, "cache_read_input_tokens", 0) or 0
        total_cache_write += getattr(u, "cache_creation_input_tokens", 0) or 0

        if response.stop_reason == "end_turn":
            break

        tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
        if not tool_use_blocks:
            break

        messages.append({"role": "assistant", "content": response.content})

        tool_results = []
        for tool in tool_use_blocks:
            result = execute_loki_tool(tool.name, tool.input)
            # Collect raw lines for upstream use
            if result and not result.startswith("["):
                all_log_lines.extend(result.splitlines())
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tool.id,
                "content": result,
            })

        messages.append({"role": "user", "content": tool_results})

    summary = next(
        (b.text for b in response.content if b.type == "text"),
        "[Log agent returned no summary]",
    )

    return {
        "summary": summary,
        "raw_lines": all_log_lines[:500],  # cap for downstream context size
        "usage": {
            "model": MODEL,
            "input_tokens": total_input,
            "output_tokens": total_output,
            "cache_read_tokens": total_cache_read,
            "cache_write_tokens": total_cache_write,
        },
    }
