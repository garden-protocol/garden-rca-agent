"""
Spark on-chain query agent.
Spark uses an EVM-compatible RPC endpoint — update tool definitions as needed
once the actual Spark RPC surface is known.
"""
import httpx
import json
from config import settings
from agents.onchain.base import BaseOnChainAgent


def _rpc(method: str, params: list) -> dict:
    """Generic JSON-RPC call to Spark node."""
    if not settings.spark_rpc_url:
        return {"error": "SPARK_RPC_URL not configured"}
    payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    try:
        resp = httpx.post(settings.spark_rpc_url, json=payload, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if data.get("error"):
            return {"error": data["error"]}
        return data.get("result", {})
    except Exception as e:
        return {"error": str(e)}


class SparkOnChainAgent(BaseOnChainAgent):

    @property
    def chain(self) -> str:
        return "spark"

    @property
    def tool_definitions(self) -> list[dict]:
        return [
            {
                "name": "get_transaction",
                "description": "Get a Spark transaction by hash.",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "tx_hash": {"type": "string", "description": "Transaction hash"},
                    },
                    "required": ["tx_hash"],
                },
            },
            {
                "name": "get_account_state",
                "description": "Get account/address state on Spark (balance, nonce, etc).",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "address": {"type": "string", "description": "Spark address"},
                    },
                    "required": ["address"],
                },
            },
            {
                "name": "get_network_info",
                "description": "Get Spark network info: current block, gas price, peer count.",
                "input_schema": {
                    "type": "object",
                    "properties": {},
                },
            },
        ]

    def execute_tool(self, tool_name: str, tool_input: dict) -> str:
        if tool_name == "get_transaction":
            result = _rpc("eth_getTransactionByHash", [tool_input["tx_hash"]])
            return json.dumps(result, default=str)

        elif tool_name == "get_account_state":
            balance = _rpc("eth_getBalance", [tool_input["address"], "latest"])
            nonce = _rpc("eth_getTransactionCount", [tool_input["address"], "latest"])
            return f"Balance: {balance}, Nonce: {nonce}"

        elif tool_name == "get_network_info":
            block = _rpc("eth_blockNumber", [])
            gas = _rpc("eth_gasPrice", [])
            peers = _rpc("net_peerCount", [])
            return f"Block: {block}, Gas price: {gas}, Peers: {peers}"

        return f"[Unknown tool: {tool_name}]"
