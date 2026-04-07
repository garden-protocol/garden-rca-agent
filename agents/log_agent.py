"""
Log Intelligence Agent.
Queries Loki for relevant log lines given an alert context,
then returns a structured log summary for use by chain specialists.
"""
import anthropic
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
    alert_context = (
        f"Order ID: {alert.order_id}\n"
        f"Alert type: {alert.alert_type}\n"
        f"Chain: {alert.chain}\n"
        f"Service: {alert.service}\n"
        f"Network: {alert.network}\n"
        f"Alert message: {alert.message}\n"
        f"Timestamp: {alert.timestamp.isoformat()}\n"
    )
    if alert.deadline:
        alert_context += f"Deadline: {alert.deadline.isoformat()}\n"
    if alert.metadata:
        alert_context += f"Metadata: {alert.metadata}\n"

    user_message = (
        f"Investigate the following alert and query Loki to find relevant logs.\n\n"
        f"Alert details:\n{alert_context}\n\n"
        f"Start by searching for the order_id across all services, "
        f"then query the specific service logs for the time window around the alert. "
        f"Look for errors, warnings, and anomalies. "
        f"Return a structured markdown report of your findings."
    )

    messages = [{"role": "user", "content": user_message}]
    all_log_lines: list[str] = []

    # Agentic loop
    while True:
        response = client.messages.create(
            model=MODEL,
            max_tokens=4096,
            thinking={"type": "adaptive"},
            output_config={"effort": "medium"},
            system=SYSTEM_PROMPT,
            tools=LOKI_TOOL_DEFINITIONS,
            messages=messages,
        )

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
    }
