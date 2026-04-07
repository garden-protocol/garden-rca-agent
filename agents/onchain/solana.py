"""Solana on-chain query agent using the Solana JSON-RPC API."""
import httpx
import json
from config import settings
from agents.onchain.base import BaseOnChainAgent


def _rpc(method: str, params: list) -> dict:
    """Make a Solana JSON-RPC call."""
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    try:
        resp = httpx.post(settings.solana_rpc_url, json=payload, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if data.get("error"):
            return {"error": data["error"]}
        return data.get("result", {})
    except Exception as e:
        return {"error": str(e)}


class SolanaOnChainAgent(BaseOnChainAgent):

    @property
    def chain(self) -> str:
        return "solana"

    @property
    def tool_definitions(self) -> list[dict]:
        return [
            {
                "name": "get_account",
                "description": "Get Solana account info for an address. Returns balance, owner, data.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "address": {"type": "string", "description": "Solana public key (base58)"},
                    },
                    "required": ["address"],
                },
            },
            {
                "name": "get_transaction",
                "description": "Get a Solana transaction by signature. Returns status, slot, logs, instructions.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "signature": {"type": "string", "description": "Transaction signature (base58)"},
                    },
                    "required": ["signature"],
                },
            },
            {
                "name": "get_recent_txs",
                "description": "Get recent transaction signatures for a Solana address.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "address": {"type": "string", "description": "Solana public key"},
                        "limit": {"type": "integer", "description": "Max signatures to return (default 10)", "default": 10},
                    },
                    "required": ["address"],
                },
            },
            {
                "name": "get_slot",
                "description": "Get current Solana slot and estimated block time.",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                },
            },
        ]

    def execute_tool(self, tool_name: str, tool_input: dict) -> str:
        if tool_name == "get_account":
            result = _rpc("getAccountInfo", [tool_input["address"], {"encoding": "base64"}])
            return json.dumps(result, default=str)

        elif tool_name == "get_transaction":
            result = _rpc("getTransaction", [
                tool_input["signature"],
                {"encoding": "json", "maxSupportedTransactionVersion": 0},
            ])
            if not result:
                return "Transaction not found (may not be confirmed yet)"
            return json.dumps(result, default=str)[:4000]

        elif tool_name == "get_recent_txs":
            result = _rpc("getSignaturesForAddress", [
                tool_input["address"],
                {"limit": tool_input.get("limit", 10)},
            ])
            if isinstance(result, list):
                lines = [f"{r.get('signature', '?')} — slot {r.get('slot')} — err={r.get('err')}" for r in result]
                return "\n".join(lines) if lines else "No recent transactions"
            return json.dumps(result, default=str)

        elif tool_name == "get_slot":
            slot = _rpc("getSlot", [])
            block_time = _rpc("getBlockTime", [slot]) if isinstance(slot, int) else "N/A"
            return f"Current slot: {slot}, block time: {block_time}"

        return f"[Unknown tool: {tool_name}]"
