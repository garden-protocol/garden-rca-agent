# Design: Extend Chain Support to All Garden Chains

**Date**: 2026-04-16
**Status**: Approved
**Approach**: Full parity (Approach A) with complete isolation — dedicated specialist + on-chain agent per chain

## Goal

Add full investigation support for 4 new chains: **tron**, **starknet**, **litecoin**, **alpen**. Each chain gets its own specialist agent, on-chain agent, config, and plumbing — matching the existing bitcoin/evm/solana pattern exactly.

**Excluded**: spark (deferred), xrpl (no repo available).

## Chain Characteristics

| Chain | Type | Repos | RPC Protocol | HTLC Type |
|-------|------|-------|-------------|-----------|
| tron | Own chain (TVM) | tron-watcher (Rust), tron-executor (TS/Bun), tron-relayer (TS/Bun) | Tron JSON-RPC (`eth_*` compatible) | GardenHTLCv3 (TRC20), `evm:htlc_erc20` schema |
| starknet | Own chain (Cairo) | garden-starknet-watcher (Rust), starknet-executor (Rust), starknet-relayer (Rust) | Starknet JSON-RPC | Cairo HTLC, SNIP-12 typed data |
| litecoin | UTXO (Bitcoin-like) | litecoin-watcher (Go), litecoin-executor (Go) | Electrs REST API | Tapscript V2 (identical to Bitcoin) |
| alpen | Bitcoin L2 (UTXO) | alpen-watcher (Go), alpen-executor (Go) | Electrs REST API | Tapscript V2 (identical to Bitcoin) |

## New Files (8 total)

### Specialist Agents

Each inherits from `BaseSpecialist`, returns its chain name, loads a default prompt with architecture reference and investigation playbook derived from knowledge docs.

| File | Chain | Key Investigation Focus |
|------|-------|------------------------|
| `agents/specialists/tron.py` | `"tron"` | GardenHTLCv3, TRC20 multicall, Effect-TS executor, Hono relayer, TronWeb, 2-block confirmations, redeemer service |
| `agents/specialists/starknet.py` | `"starknet"` | Cairo HTLC, SNIP-12 signatures, multicall via starknet-rs, NonceCounter, event selectors, 30min executor cache |
| `agents/specialists/litecoin.py` | `"litecoin"` | Tapscript V2 HTLC (same as Bitcoin), ltcsuite/ltcd, BatcherWallet, RBF, SACP instant refund, no dedicated relayer |
| `agents/specialists/alpen.py` | `"alpen"` | Bitcoin L2 (UTXO), Tapscript V2, Electrs indexer, hybrid executor (also handles EVM), SACP instant refund |

### On-Chain Agents

Each inherits from `BaseOnChainAgent`, defines its own `tool_definitions` and `execute_tool()`. Fully isolated — no shared base beyond `BaseOnChainAgent`.

#### `agents/onchain/tron.py` — TronOnChainAgent

**RPC**: Tron JSON-RPC (`eth_*` compatible). Config: `tron_rpc_url`.
**Implementation**: Uses `httpx` for raw JSON-RPC calls (Tron speaks `eth_getBalance`, `eth_call`, `eth_getLogs`, `eth_getTransactionByHash`, `eth_getTransactionReceipt`, `eth_blockNumber`).

Tools:
| Tool | Purpose |
|------|---------|
| `get_native_balance` | Get TRX balance of a Tron address (via `eth_getBalance`) |
| `get_trc20_balance` | Get TRC20 token balance (via `eth_call` to `balanceOf`) |
| `get_transaction` | Get transaction by hash (via `eth_getTransactionByHash`) |
| `get_transaction_receipt` | Get receipt for mined tx (via `eth_getTransactionReceipt`) |
| `get_htlc_order_state` | Read GardenHTLCv3 order state (via `eth_call` to `getOrder` selector `0x9c3f1e90`) |
| `get_logs` | Fetch contract event logs (via `eth_getLogs`) |
| `get_block_number` | Get current block number (via `eth_blockNumber`) |

System prompt guides the LLM on Tron address format (Base58 vs hex), GardenHTLCv3 swap struct (192 bytes), and BALANCE_INSUFFICIENT/HTLC_REDEEMED keyword protocol.

#### `agents/onchain/starknet.py` — StarknetOnChainAgent

**RPC**: Starknet JSON-RPC. Config: `starknet_rpc_url`.
**Implementation**: Uses `httpx` for raw Starknet JSON-RPC calls (`starknet_call`, `starknet_getTransactionByHash`, `starknet_getTransactionReceipt`, `starknet_blockNumber`, `starknet_getEvents`).

Tools:
| Tool | Purpose |
|------|---------|
| `get_account_balance` | Get ETH/token balance via `starknet_call` to `balanceOf` |
| `get_transaction` | Get transaction by hash |
| `get_transaction_receipt` | Get receipt (execution_status: SUCCEEDED/REVERTED) |
| `get_htlc_order_state` | Read HTLC state via `starknet_call` to `get_order` (returns 7 Felts: is_fulfilled, initiator, redeemer, initiated_at, timelock, amount_low, amount_high) |
| `get_block_number` | Current block number |
| `get_events` | Fetch contract events by selector |

System prompt guides the LLM on Felt encoding, SNIP-12 typed data, event selector format, and the keyword protocol.

#### `agents/onchain/litecoin.py` — LitecoinOnChainAgent

**RPC**: Electrs REST API. Config: `litecoin_electrs_url`.
**Implementation**: Uses `httpx` for Electrs REST calls.

Tools:
| Tool | Purpose |
|------|---------|
| `get_address_balance` | Get LTC balance via `/address/{addr}/utxo` (sum UTXO values) |
| `get_address_utxos` | Get UTXOs for an address (HTLC spent check: empty = spent) |
| `get_transaction` | Get tx details via `/tx/{txid}` |
| `get_tip_block_height` | Current block height via `/blocks/tip/height` |
| `get_fee_estimates` | Fee rates via `/fee-estimates` |

System prompt guides the LLM on Taproot HTLC witness analysis (redeem leaf vs refund leaf vs multisig leaf), UTXO-based HTLC detection, and Litecoin address format (`ltc1p...`).

#### `agents/onchain/alpen.py` — AlpenOnChainAgent

**RPC**: Electrs REST API. Config: `alpen_electrs_url`.
**Implementation**: Uses `httpx` for Electrs REST calls (same API shape as Litecoin but different endpoint).

Tools:
| Tool | Purpose |
|------|---------|
| `get_address_balance` | Get BTC balance via `/address/{addr}/utxo` |
| `get_address_utxos` | Get UTXOs (HTLC spent check) |
| `get_transaction` | Get tx details via `/tx/{txid}` |
| `get_tip_block_height` | Current block height |
| `get_fee_estimates` | Fee rates |

System prompt guides the LLM on Bitcoin L2 nature, Taproot HTLC structure (same as Bitcoin/Litecoin), `ContractAddress = "primary"` keyword, and Electrs API patterns.

## Modified Files (7 total)

### `config.py`

New settings fields:

```python
# RPC endpoints
tron_rpc_url: str = ""
starknet_rpc_url: str = ""
litecoin_electrs_url: str = ""
alpen_electrs_url: str = ""

# Repo paths — tron
repo_tron_executor: str = "/opt/repos/tron-executor"
repo_tron_watcher: str = "/opt/repos/tron-watcher"
repo_tron_relayer: str = "/opt/repos/tron-relayer"

# Repo paths — starknet
repo_starknet_executor: str = "/opt/repos/starknet-executor"
repo_starknet_watcher: str = "/opt/repos/starknet-watcher"
repo_starknet_relayer: str = "/opt/repos/starknet-relayer"

# Repo paths — litecoin
repo_litecoin_executor: str = "/opt/repos/litecoin-executor"
repo_litecoin_watcher: str = "/opt/repos/litecoin-watcher"

# Repo paths — alpen
repo_alpen_executor: str = "/opt/repos/alpen-executor"
repo_alpen_watcher: str = "/opt/repos/alpen-watcher"

# Branch overrides
branch_tron_executor: str = "staging"
branch_tron_watcher: str = "staging"
branch_tron_relayer: str = "staging"
branch_starknet_executor: str = "staging"
branch_starknet_watcher: str = "staging"
branch_starknet_relayer: str = "staging"
branch_litecoin_executor: str = "staging"
branch_litecoin_watcher: str = "feat/ltcsuite"
branch_alpen_executor: str = "feat/alpen"
branch_alpen_watcher: str = "feat/alpen"

# Relayer addresses
relayer_address_tron: str = ""
relayer_address_starknet: str = ""
relayer_address_litecoin: str = ""
relayer_address_alpen: str = ""

# Min gas balances (chain-native units)
min_tron_gas_balance: int = 10_000_000          # 10 TRX in SUN
min_starknet_gas_balance: int = 1_000_000_000_000_000  # 0.001 ETH in wei
min_litecoin_gas_balance: int = 100_000          # 0.001 LTC in litoshis
min_alpen_gas_balance: int = 10_000              # 10k satoshis
```

Update these methods to include all 4 new chains:
- `relayer_address(chain)` — add tron, starknet, litecoin, alpen entries
- `min_gas_balance(chain)` — add tron, starknet, litecoin, alpen entries
- `repo_paths(chain)` — add tron (executor, watcher, relayer), starknet (executor, watcher, relayer), litecoin (executor, watcher), alpen (executor, watcher)
- `repo_branches(chain)` — matching branch overrides per component
- `gitea_repos(chain)` — add Gitea repo name + branch tuples for all new chain components

### `main.py`

```python
SUPPORTED_CHAINS = {"bitcoin", "evm", "solana", "tron", "starknet", "litecoin", "alpen"}
```

### `tools/orders_api.py`

Add to `CHAIN_MAP`:
```python
"tron":      "tron",
"starknet":  "starknet",
"litecoin":  "litecoin",
"alpen":     "alpen",
```

### `models/alert.py`

```python
chain: Literal["bitcoin", "evm", "solana", "tron", "starknet", "litecoin", "alpen"]
```

### `agents/orchestrator.py`

Import and register all new agents:
```python
from agents.specialists.tron import TronSpecialist
from agents.specialists.starknet import StarknetSpecialist
from agents.specialists.litecoin import LitecoinSpecialist
from agents.specialists.alpen import AlpenSpecialist
from agents.onchain.tron import TronOnChainAgent
from agents.onchain.starknet import StarknetOnChainAgent
from agents.onchain.litecoin import LitecoinOnChainAgent
from agents.onchain.alpen import AlpenOnChainAgent

_SPECIALISTS = {
    "bitcoin": BitcoinSpecialist(),
    "evm": EVMSpecialist(),
    "solana": SolanaSpecialist(),
    "tron": TronSpecialist(),
    "starknet": StarknetSpecialist(),
    "litecoin": LitecoinSpecialist(),
    "alpen": AlpenSpecialist(),
}

_ONCHAIN_AGENTS = {
    "bitcoin": BitcoinOnChainAgent(),
    "evm": EVMOnChainAgent(),
    "solana": SolanaOnChainAgent(),
    "tron": TronOnChainAgent(),
    "starknet": StarknetOnChainAgent(),
    "litecoin": LitecoinOnChainAgent(),
    "alpen": AlpenOnChainAgent(),
}
```

### `tools/loki.py`

Update `_PRIMARY_SERVICE_MAP` — add any missing entries for new chains:
```python
("relayer", "tron"):     "/tron-relayer-mainnet",      # already exists
("watcher", "tron"):     "/tron-watcher",              # already exists
("watcher", "starknet"): "/starknet-watcher-mainnet",  # already exists
("relayer", "starknet"): "/starknet-relayer-mainnet",
("watcher", "litecoin"): "/litecoin-services-mainnet", # already exists
("watcher", "alpen"):    "/alpen-watcher-mainnet",
```

Update `_SOLVER_SERVICE_MAP` — already has tron, starknet, litecoin, alpen entries. Verify.

Update `search_by_service` tool definition `enum` to include `"alpen"`:
```python
"enum": ["bitcoin", "evm", "solana", "tron", "starknet", "spark", "litecoin", "alpen"],
```

### `agents/explore_agent.py`

Update `_build_repo_registry()` to loop over all chains:
```python
for chain in ("bitcoin", "evm", "solana", "tron", "starknet", "litecoin", "alpen"):
```

Add to `_ALIASES`:
```python
"tron-executor": "tron-executor",
"tron-watcher": "tron-watcher",
"tron-relayer": "tron-relayer",
"starknet-executor": "starknet-executor",
"starknet-watcher": "garden-starknet-watcher",
"garden-starknet-watcher": "garden-starknet-watcher",
"starknet-relayer": "starknet-relayer",
"litecoin-executor": "litecoin-executor",
"litecoin-watcher": "litecoin-watcher",
"alpen-executor": "alpen-executor",
"alpen-watcher": "alpen-watcher",
```

Add to `_KEYWORD_HINTS`:
```python
"tron executor": "tron-executor",
"tron watcher": "tron-watcher",
"tron relay": "tron-relayer",
"trc20": "tron-executor",
"starknet executor": "starknet-executor",
"starknet watcher": "garden-starknet-watcher",
"starknet relay": "starknet-relayer",
"cairo": "starknet-executor",
"snip-12": "starknet-executor",
"litecoin executor": "litecoin-executor",
"litecoin watcher": "litecoin-watcher",
"ltc": "litecoin-executor",
"alpen executor": "alpen-executor",
"alpen watcher": "alpen-watcher",
```

## Implementation Order

1. **Config** (`config.py`) — all new settings, repo paths, gitea mappings, method updates
2. **Plumbing** — `CHAIN_MAP`, `SUPPORTED_CHAINS`, `alert.py` Literal, Loki mappings
3. **On-chain agents** — tron, starknet, litecoin, alpen (4 files)
4. **Specialists** — tron, starknet, litecoin, alpen (4 files)
5. **Orchestrator** — import and register all new agents
6. **Explore agent** — aliases, keyword hints, repo registry loop
7. **Verification** — start server, test health endpoint, verify chain list
