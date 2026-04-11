# Solver Ecosystem Knowledge Base

Covers 5 solver-layer services that sit above chain-specific executors/watchers/relayers.
These services orchestrate order processing, liquidity tracking, and solver configuration —
failures here cascade to all chains.

---

## 1. Service Architecture Overview

### What Each Service Does

**solver-engine** (Rust, port 7070)
The core order processing loop. Runs every 2 seconds: fetches pending orders from the orders API,
maps them to chain-specific actions (initiate/redeem/refund) via an OrderMapper, validates them,
and dispatches to the appropriate chain executor. Each order is locked in a Moka cache (600s TTL)
to prevent duplicate processing. A background status watcher polls the executor's `/status/{action_id}`
every 1 second and unlocks the order on success/failure (or after 2 minutes of NotFound).

Executors register with solver-engine via `POST /register` providing their URL and supported chains.
The engine uses a dual registry (current + legacy format) for backwards compatibility.

Key constants:
- Main loop interval: 2000ms
- Status watcher poll: 1000ms
- NotFound timeout: 120s (unlocks order for retry)
- Lock TTL: 600s
- Max agentic turns (for tool use): 25

**solver-comms** (Rust, configurable port)
The liquidity monitor. Continuously polls on-chain balances for each configured chain/asset using
chain-specific fetchers (EVM, Solana, Bitcoin, Starknet, Sui, Tron, XRPL, Zcash, Spark). For each
asset, computes `virtual_balance = balance - committed_funds` where committed funds come from the
orderbook API (`/solver/{address}/committed-funds?chain={chain}&asset={asset}`).

Results are cached in a Moka async cache with configurable TTL. Every 20 seconds, pushes current
liquidity/chains/policy to the solver-aggregator via HTTP POST. Also serves these via its own HTTP
endpoints: `GET /liquidity`, `/policy`, `/chains`, `/health`.

Key constants:
- Per-asset fetch timeout: 10 seconds
- Watcher warmup delay: 5 seconds (before serving HTTP)
- Aggregator sync interval: 20 seconds
- Cache max capacity: 10,000 entries

**solver-agg-v2** (Rust, port 3000)
The solver aggregator — centralized registry where solvers register and store their configuration.
Solver credentials (API key hash) persist in PostgreSQL. Liquidity, chains, and policy data are
stored in **ephemeral Moka caches** (120-180s TTL) — all non-credential data is lost on restart
or if the solver-comms instance stops pushing updates.

API key format: `sr_live_{solver_id}_{64_char_hex_secret}` (SHA256 hashed for storage).
Registration requires a shared `register_secret`.

Endpoints:
- `POST /solvers` — register solver (requires register_secret)
- `GET /solvers/me` — validate API key (Bearer token)
- `POST|GET /liquidity` — store/fetch solver liquidity
- `POST|GET /chains` — store/fetch supported chains
- `POST|GET /policies` — store/fetch trading policies

Key limits:
- DB connection pool: 20 max
- Liquidity cache: 10K entries, 180s TTL
- Chain cache: 10K entries, 120s TTL
- Policy cache: 10K entries, 120s TTL

**solver (solverd)** (Rust CLI)
The solver daemon — a Docker Compose orchestrator that starts all solver services. Reads
configuration from `~/.solverd/{mode}/Solver.toml` (mode = mainnet/testnet/staging) and loads
Garden network settings from CDN (`https://gos.btcfi.wtf/solverd/{mode}.json`).

Startup sequence (strict order, failures cascade):
1. Start solver-engine (port 7070)
2. Start all chain executors (ports 5050-5059, one per chain type)
3. Unlock executor keystores via HTTP POST to unlock ports (404x)
4. Fetch account info from each executor (`GET /accounts`)
5. Start solver-comms (uses account info for chain configs)
6. Optionally start rebalancer + promtail

Solver.toml key fields:
- `api_key`: `sr_live_{solver_id}_{secret}` — identifies the solver
- `fee`: basis points
- `price_drop_threshold`: slippage tolerance (default 0.01 = 1%)
- `[[chains]]`: array of chain configs with RPC URLs, assets, rebalance opts
- `[policy]`: blacklist pairs, whitelist overrides, max limits per asset
- `[screener]`: TRM Labs AML/CFT config (risk_score_limit, batch_size, cache TTL)
- `[rebalancer]`: Astra rebalancing config (poll_interval, min/optimal/max per asset)

Supported chains: Bitcoin, Ethereum, Arbitrum, Base, Polygon, Avalanche, StarkNet, Solana,
XRPL, Litecoin, Alpen, Tron, Spark.

**solver-dashboard** (Rust backend + React frontend)
Management UI for solver operators. Backend: Axum + SQLite (audit logs, members), JWT auth with
2FA (TOTP + Email). Roles: Admin (full control), Operator (start/stop/config), Viewer (read-only).

Key pages: dashboard metrics, pending orders, liquidity pools, analytics, audit logs, config
management, policy editor, member management, real-time Docker log streaming (SSE).

Backend talks to Garden Finance APIs for orders/liquidity and manages the solver daemon process
via PTY for interactive I/O.

---

## 2. Inter-Service Communication

```
Garden CDN ──> solver daemon (loads GardenSettings at startup)
                   │
                   ├── Docker Compose ──> solver-engine (port 7070)
                   │                          │
                   │                          ├── Fetches pending orders from orders API
                   │                          ├── Maps orders to chain actions
                   │                          └── Dispatches to chain executors:
                   │                              ├── evm-executor    (port 5050)
                   │                              ├── btc-executor    (port 5051)
                   │                              ├── starknet-exec   (port 5053)
                   │                              ├── solana-executor (port 5054)
                   │                              ├── xrpl-executor   (port 5055)
                   │                              ├── ltc-executor    (port 5056)
                   │                              ├── alpen-executor  (port 5057)
                   │                              ├── spark-executor  (port 5058)
                   │                              └── tron-executor   (port 5059)
                   │
                   ├── Docker Compose ──> solver-comms
                   │                          │
                   │                          ├── Polls on-chain balances (per chain RPC)
                   │                          ├── Queries orderbook for committed funds
                   │                          ├── Computes virtual_balance
                   │                          └── Pushes to solver-agg-v2 every 20s
                   │
                   ├── Docker Compose ──> rebalancer (optional, port 6000)
                   │                          └── Astra API for cross-chain rebalancing
                   │
                   └── Docker Compose ──> promtail (optional)
                                              └── Forwards logs to Loki

solver-agg-v2 (port 3000) ←── solver-comms (liquidity/chains/policy)
                           ←── external consumers (query solver configs)

solver-dashboard ──> solver daemon (process management)
                 ──> Garden Finance API (orders, liquidity, assets)
```

---

## 3. Order Processing Pipeline (solver-engine detail)

```
Every 2 seconds:
  1. fetch_all_orders()
     - Get all registered executors from ChainExecutorCache
     - For each executor: fetch pending orders filtered by solver_id
     - De-duplicate by create_id
     - Require at least 1 successful fetch

  2. unlock_updated_orders()
     - Check locked orders cache
     - If order action has tx_hash set (completed), remove from lock

  3. filter_unlocked_orders()
     - Remove already-locked orders from the batch

  4. map_orders()
     - Call OrderMapper.map() for each order
     - Returns ChainAction (Initiate/Redeem/Refund/NoOp)
     - Validate initiate actions via executor
     - Skip NoOp actions
     - Group by destination chain

  5. send_orders_to_executors()
     - For each chain group:
       - Get executor from cache
       - POST /execute with batch of OrderWithAction
       - Expects 202 ACCEPTED
       - Lock each order in cache (600s TTL)
       - Spawn status watcher per action

Status watcher (per action):
  Every 1 second:
    - GET /status/{action_id} from executor
    - NotFound → track duration, break after 120s
    - Pending → continue polling
    - Success → log tx_hash, exit
    - Failed → unlock order (will retry next cycle)
```

---

## 4. Liquidity Computation (solver-comms detail)

```
For each configured chain:
  For each asset on that chain:
    1. Fetch raw balance via chain-specific LiquidityFetcher
       - EVM: alloy provider + balanceOf RPC/contract calls
       - Solana: solana-client + associated token accounts
       - Bitcoin: REST API summing UTXOs
       - Others: chain-specific REST/RPC calls
    2. Fetch committed funds: GET {orderbook_url}/solver/{address}/committed-funds
    3. virtual_balance = max(0, balance - committed_funds)
    4. readable_balance = format to 8 decimal places
    5. Cache as AssetLiquidity keyed by AssetId (chain:token)
```

Stale liquidity detection:
- If solver-comms stops or RPC fails, cache entries expire per TTL
- Aggregator cache (120-180s) goes stale independently
- Orders can be matched against non-existent liquidity if sync breaks

---

## 5. Common Failure Modes

### solver-engine failures
| Symptom | Likely cause | Investigation |
|---------|-------------|---------------|
| Orders not being processed | No executors registered | Check `/register` calls at startup, executor health |
| Duplicate order execution | Lock cache eviction before completion | Check Moka cache capacity, TTL settings |
| Order stuck in "locked" state | Status watcher died or executor unreachable | Check executor HTTP connectivity, 60s request timeout |
| Order mapping failures | Price drop > threshold, fiat provider down | Check price_drop_threshold config, fiat API |
| All executors send failed | Network partition or executor crash | Check Docker container status, port availability |

### solver-comms failures
| Symptom | Likely cause | Investigation |
|---------|-------------|---------------|
| Stale liquidity in aggregator | solver-comms not pushing (crash/restart) | Check solver-comms container logs |
| Zero virtual_balance | Committed funds API returning inflated values | Check orderbook committed-funds endpoint |
| Balance fetch timeout | RPC node unresponsive | Check chain RPC health, 10s timeout |
| Aggregator shows no solver | API key auth failure or aggregator restart | Check solver-agg-v2 logs, cache state |

### solver-agg-v2 failures
| Symptom | Likely cause | Investigation |
|---------|-------------|---------------|
| Solver config disappeared | Service restarted (ephemeral cache) | Check pod/container restarts |
| Registration fails | Invalid register_secret | Check aggregator config |
| Stale policy/liquidity | solver-comms stopped pushing or cache TTL expired | Check solver-comms health, 120-180s TTL |
| DB connection errors | Pool exhausted (20 max) | Check concurrent auth requests |

### solverd (daemon) failures
| Symptom | Likely cause | Investigation |
|---------|-------------|---------------|
| Startup fails at step 3 | Keystore unlock failed (wrong password) | Check unlock endpoint response |
| Executors crash after start | Invalid RPC URLs in Solver.toml | Check chain RPC connectivity |
| Solver-comms has no accounts | Account info fetch failed in step 4 | Check executor `/accounts` responses |
| CDN config load fails | Network issue or invalid mode | Check `gos.btcfi.wtf` availability |

---

## 6. Configuration Reference

### Solver.toml structure
```toml
solver_name = "my-solver"
api_key = "sr_live_{solver_id}_{secret}"
fee = 30                          # basis points
price_drop_threshold = 0.01       # 1% slippage tolerance
telemetry = true

[[chains]]
name = "ethereum"
rpc = "https://eth-mainnet.g.alchemy.com/v2/..."
assets = ["ethereum:wbtc", "ethereum:cbbtc"]

  [chains.chain_params]
  type = "Evm"

  [[chains.rebalance_opts]]
  asset_id = "ethereum:wbtc"
  min = "0.1"
  optimal = "0.5"
  max = "1.0"

[[chains]]
name = "bitcoin"
rpc = "https://bitcoin-rpc.example.com"
assets = ["bitcoin:btc"]

  [chains.chain_params]
  type = "Bitcoin"
  node_url = "https://bitcoin-node.example.com"

[policy]
default = "open"                  # or "closed"
blacklist_pairs = []
whitelist_overrides = []
max_limits = {}

[rebalancer]
poll_interval_secs = 300

  [rebalancer.astra]
  account_id = "..."
  api_key = "..."

[screener]
trm_api_key = "..."
risk_score_limit = 10
batch_size = 10
address_cache_ttl_hours = 24
```

### GardenSettings (from CDN)
```json
{
  "fiat_url": "...",
  "orderbook_url": "...",
  "pending_orders_url": "...",
  "assets_url": "...",
  "solver_agg_url": "...",
  "logs_middleware_url": "...",
  "swap_expiry_seconds": 3600,
  "chain_settings": { ... }
}
```

---

## 7. Key Types

### solver-engine
- `MatchedOrderVerbose` — full order with source/destination swaps (from garden-rs)
- `ChainAction` — blockchain action (side + action type: Initiate/Redeem/Refund/NoOp)
- `OrderWithAction` — pairing of order + action for executor dispatch
- `ExecutionStatus` — NotFound | Pending | Success { tx_hash } | Failed { reason }
- `ValidationType` — SourceInitiate/SourceRedeem/SourceRefund/DestinationInitiate/DestinationRedeem/DestinationRefund

### solver-comms
- `AssetLiquidity` — { asset, address, balance, virtual_balance, readable_balance }
- `SolverLiquidity` — { solver_id, liquidity: Vec<AssetLiquidity> }
- `SolverPolicyConfig` — { default, isolation_groups, blacklist_pairs, whitelist_overrides, fees, overrides }
- `SupportedChain` — { chain, assets, solver_account }

### solver-agg-v2
- Same types as solver-comms (receives via POST)
- `SolverCredentials` — { solver_id, api_key_hash, created_at } (PostgreSQL)

---

## 8. Debugging Checklist

When a stuck order involves solver-layer issues:

1. **Is solver-engine running and processing?**
   - Check if executors are registered (engine logs "no executors" if empty)
   - Check main loop is cycling (should log every 2s)
   - Check if order is locked in cache (won't be retried until lock expires/unlocked)

2. **Is the order being mapped correctly?**
   - OrderMapper uses fiat provider for price validation
   - Check price_drop_threshold — if market moved >1%, orders get skipped
   - Check if fiat provider API is responsive

3. **Is the executor receiving and executing?**
   - solver-engine sends via `POST /execute`, expects 202
   - Check executor container status and port availability
   - Check executor logs for the specific action_id

4. **Is liquidity accurate?**
   - Check solver-comms balance fetch (RPC health per chain)
   - Check committed funds from orderbook API
   - Check aggregator cache freshness (120-180s TTL)
   - If aggregator restarted, all cached data is gone until solver-comms re-pushes

5. **Is the solver daemon healthy?**
   - Check Docker Compose status for all services
   - Check startup logs for any failed steps
   - Verify Solver.toml has correct RPC URLs and API key
