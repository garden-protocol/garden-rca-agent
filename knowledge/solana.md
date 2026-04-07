# Garden Protocol — Solana Service Knowledge Document

> For use by chain-specialist AI diagnosing production incidents on Garden's Solana integration.
> All file paths prefixed with their repo name (executor/, relayer/, watcher/, native-swaps/, spl-swaps/).

---

## 1. Service Architecture Overview

### Services

There are three off-chain services (executor, relayer, watcher) and two on-chain Anchor programs (native-swaps, spl-swaps).

| Service | Role |
|---------|------|
| **executor** | Initiates destination-side Solana swaps; redeems source-side Solana swaps when secret arrives; refunds/instant-refunds Solana swaps when appropriate |
| **relayer** | Builds and submits transactions on behalf of users (fee sponsorship); auto-redeem and auto-refund background loops; HTTP API for frontend/SDK |
| **watcher** | Polls Solana RPC for on-chain events (Initiated, Redeemed, Refunded, InstantRefunded) and writes them into the Garden PostgreSQL database |

### How They Interact

```
User/SDK
  │
  ▼
relayer (HTTP server on port 5014 by default)
  │  buildInitiateTx / buildNativeInitTx  → unsigned tx returned to user
  │  initiate(orderId, serializedTx)      → relayer co-signs & submits
  │  redeem(orderId, secret)              → relayer submits redeem tx
  │
Solana RPC ◄────────────────────────────────────────────────────────┐
  │                                                                  │
  ▼                                                                  │
watcher (two instances: native + SPL)                               │
  │  getSignaturesForAddress → getTransaction → parseLogs           │
  │  → updateEvents (DB: swaps.initiate_tx_hash, redeem_tx_hash, refund_tx_hash)
  │  → updateConfirmations (DB: swaps.current_confirmations)         │
  ▼                                                                  │
Garden PostgreSQL DB                                                  │
  │                                                                  │
  ▼                                                                  │
executor (polls pending_orders_api every 5s by default)             │
  │  getPendingOrders(chain_identifier, solver_id)                   │
  │  for each order:                                                 │
  │    → handleInitiate  (dest chain = solana, source initiated)  ──►│
  │    → handleRedeem    (source chain = solana, secret available) ──►│
  │    → handleRefund    (dest chain = solana, timelock expired)   ──►│
  │    → handleInstantRefund (source chain = solana, dest refunded)──►│
  │                                                                  │
  ▼                                                                  │
executor registry (register + validate cross-chain source initiate)  │
```

### Order Lifecycle on Solana

1. **Order created** off-chain in Garden orderbook.
2. **Source initiation** (non-Solana chain or Solana as source):
   - If Solana is source: user signs initiate tx via relayer's `buildInitiateTxUsingOrderId` / `buildInitiateTx`; relayer co-signs and submits.
   - Watcher picks up the `Initiated` event, sets `initiate_tx_hash` + `initiate_block_number` in DB.
3. **Destination initiation** (Solana as destination):
   - executor's `handleInitiate` runs after verifying: source chain has a confirmed initiate, price protection passes, validator service confirms source chain executor agrees.
   - executor calls `nativeSolanaInitiate` or `splInitiate` on-chain.
   - Watcher picks up the `Initiated` event on the destination side.
4. **Redemption**:
   - When secret is revealed on dest chain, executor's `handleRedeem` calls `nativeSolanaRedeem` or `splRedeem`.
   - For user-facing source Solana swaps: relayer's auto-redeem loop or the `POST /redeem` endpoint is called.
   - Watcher picks up `Redeemed` event, writes `redeem_tx_hash` + `secret` to DB.
5. **Refund** (timelock expired, no redeem):
   - executor's `handleRefund` (dest-side Solana) when `currentSlot >= initiate_block_number + timelock`.
   - relayer's auto-refund loop handles source-side Solana refunds.
   - Watcher picks up `Refunded` or `InstantRefunded`, writes `refund_tx_hash`.
6. **Instant refund** (cooperative early exit):
   - executor's `handleInstantRefund`: triggered when counterparty (COBI/filler) already refunded on dest chain (`destination_swap.refund_tx_hash` set) but source hasn't refunded, OR when deadline + 30-min buffer has passed and no dest initiate.
   - Requires redeemer's signature on-chain.

### Native SOL vs SPL Token Swaps

| Dimension | Native (solana-native-swaps) | SPL (solana-spl-swaps) |
|-----------|-----------------------------|-----------------------|
| Program ID (devnet/example) | `6eksgdCnSjUaGQWZ6iYvauv1qzvYPF33RTGTM1ZuyENx` | `2WXpY8havGjfRxme9LUxtjFHTh1EfU3ur4v6wiK4KdNC` |
| Asset transferred | Lamports (native SOL) | SPL/Token-2022 tokens |
| Escrow mechanism | SOL locked in the swap_account PDA itself | Tokens locked in per-mint `token_vault` PDA |
| Vault PDA seeds | N/A (account is the vault) | `[mint.key()]` |
| Swap PDA seeds | `[b"swap_account", initiator, secret_hash]` | `[initiator, secret_hash]` |
| Rent sponsor | `rent_payer` account (can be same as initiator) | `sponsor` account (relayer can pay for gasless) |
| `token_address` field | `"primary"` | Mint pubkey |
| Initiate signature requirement | initiator + rent_payer must sign | initiator + sponsor must sign |
| Redeem signature requirement | None (permissionless) | None (permissionless) |
| Refund signature requirement | None (permissionless, after expiry) | None (permissionless, after expiry) |
| Instant refund signature | redeemer must sign | redeemer must sign |
| Token program resolution | N/A | `TokenProgramIdResolver` detects TOKEN vs TOKEN_2022 |
| Identity PDA | Not used | Empty-seeds PDA `[]` used as vault authority |
| `getOrCreateAssociatedTokenAccount` | N/A | Relayer creates redeemer ATA on first redeem if missing |

---

## 2. Key Files and Their Roles

### Executor

| File | Role |
|------|------|
| `executor/main.ts` | Entry point: loads config, creates `OrderProcessor`, registers with executor registry, runs main polling loop every `executor_sleep_duration` ms |
| `executor/executorUtils/factory.ts` | `createOrderProcessor()` wires all services together |
| `executor/executorUtils/orderProcessor.ts` | `OrderProcessor.processSolanaMatchedOrders()`: fetches pending orders, iterates, delegates to `OrderHandler.processOrder()` |
| `executor/executorUtils/orderHandler.ts` | Core decision logic: blacklist check → redeem check → initiate check (with price protection + validator) → instant refund check → refund check |
| `executor/blockchain/solanaService.ts` | `SolanaService`: wraps Anchor programs, implements all on-chain calls (initiate, redeem, refund, instantRefund) for both native and SPL |
| `executor/blockchain/txConfirmation.ts` | `confirmTransaction()`: HTTP-only polling at 400ms intervals; throws on on-chain error or blockhash expiry |
| `executor/blockchain/quoteService.ts` | `QuoteService`: fetches token prices from quote server; 10s cache; used for price protection |
| `executor/blockchain/tokenProgramIdResolver.ts` | `TokenProgramIdResolver`: resolves mint's owner to TOKEN or TOKEN_2022 program; caches results |
| `executor/executorUtils/actionsCache.ts` | `ActionsCache`: LRU caches with 10-minute TTL per action type (initiate/redeem/refund/instantRefund); prevents double-submission |
| `executor/executorUtils/ordersProvider.ts` | `PendingOrdersProvider`: HTTP GET `{pending_orders_api_url}/{chain_identifier}?solver={solver_id}` |
| `executor/executorUtils/validatorService.ts` | `ValidatorService`: POST to `{executor_registry_url}/{chain}/validate` with 5s timeout; validates source chain initiated before Solana initiate |
| `executor/server/server.ts` | Express server on `server_port` (default 8080): `GET /health`, `POST /validate`, `GET /accounts` |
| `executor/server/handler.ts` | `validateOrderHandler`: checks swap on-chain; `getAddressHandler`: returns executor pubkey |
| `executor/server/validator.ts` | `validateSwap()`: fetches PDA, validates amount + expiry slot + initiator + redeemer match order data |
| `executor/server/types.ts` | `ValidationType` enum, `AppState`, `SwapInfo`, helper functions |
| `executor/config/configLoader.ts` | Zod-validated config loader from `config.json` |
| `executor/utils/register.ts` | `register()`: POSTs to executor registry with 20 retries, exponential backoff starting at 1s; `getChainName()` maps chain IDs |
| `executor/utils/keystoreUtils.ts` | `decryptPrivateKey()`: decrypts keystore for filler keypair |

### Relayer

| File | Role |
|------|------|
| `relayer/src/main.ts` | Entry point: initializes programs, starts Server, optionally starts Redeemer and Refunder |
| `relayer/src/relayer.ts` | Core relay logic: `buildNativeInitTx`, `buildInitiateTx`, `buildInitiateTxUsingOrderId`, `initiate`, `redeem`; error classes |
| `relayer/src/solana.ts` | `Solana`: native program wrapper (`getInitiateTx`, `redeem`, `refund`) |
| `relayer/src/solana-spl.ts` | `SolanaSpl`: SPL program wrapper; `getSwapParams` decodes serialized tx; `getOrCreateAssociatedTokenAccount` |
| `relayer/src/redeemer.ts` | `Redeemer`: auto-redeem loop; fetches pending orders + secrets; calls `redeemAll`; 1h TTL cache per swap_id |
| `relayer/src/refunder.ts` | `Refunder`: auto-refund loop; `filterRefundable` checks `currentSlot >= initiate_block_number + timelock` |
| `relayer/src/server.ts` | Express server: `POST /redeem`, `POST /initiate`, `POST /build-initiate-tx`, `POST /native/build-init`, `PATCH /:version/:orderId`, `GET /:version/:orderId` |
| `relayer/src/storage.ts` | `PgStore`: `getSwap(orderId, isSourceSwap)` SQL query; DB connection pool up to 150 connections |
| `relayer/src/config.ts` | Config from `Settings.toml` via vault-ts; auto_redeem and auto_refund sections |
| `relayer/src/utils.ts` | `confirmTransaction()`: same pattern as executor (400ms poll, HTTP-only) |
| `relayer/src/token-program-id-resolver.ts` | Same TOKEN vs TOKEN_2022 resolver as executor |

### Watcher

| File | Role |
|------|------|
| `watcher/src/main.ts` | Entry point: two `Watcher` instances (native + SPL) + `updateConfirmations` loop, all run concurrently |
| `watcher/src/watcher.ts` | `Watcher.run()`: main polling loop; `processFrom()`: fetches txns from RPC, parses events, calls `storage.updateEvents()` |
| `watcher/src/watcher.ts` | `updateConfirmations()`: fetches unconfirmed txns (confirmations < 2), updates DB |
| `watcher/src/event-parser.ts` | `SolanaEventParser.parseEvents()`: uses Anchor `EventParser` to decode `Initiated`, `Redeemed`, `Refunded`, `InstantRefunded` from tx logs |
| `watcher/src/solana-client.ts` | `SolanaClient`: `getTransactions()` (newest-first pagination), `getConfirmations()` (chunked by 256), `getBlockTime()` |
| `watcher/src/storage.ts` | `PgStore`: `updateEvents()`, `updateInit()`, `updateRedeem()`, `updateRefund()`, `fetchUnconfirmedTransactions()`, `updateConfirmations()` |
| `watcher/src/config.ts` | Config from `Settings.toml`: poll intervals, program IDs, `start_after_transaction` cursor |

### On-Chain Programs

| File | Role |
|------|------|
| `native-swaps/programs/solana-native-swaps/src/lib.rs` | Anchor program: `initiate`, `redeem`, `refund`, `instant_refund`; `SwapAccount` struct; `SwapError` enum |
| `spl-swaps/programs/solana-spl-swaps/src/lib.rs` | Anchor program: same interface but for SPL/Token-2022 tokens; `token_vault` PDA; `identity_pda` as vault authority |

---

## 3. Critical Functions

### Order Initiation

**executor/executorUtils/orderHandler.ts — `handleInitiate(order)`**
- Called when: `basicInitiateChecks()` passes AND deadline not exceeded AND price protection passes AND `ValidatorService.isValid()` returns true
- `basicInitiateChecks()` requires:
  - `destination_swap.chain.includes("solana")`
  - `source_swap.initiate_tx_hash` is non-empty
  - `destination_swap.initiate_tx_hash` is empty
  - `source_swap.refund_tx_hash` is empty
  - `source_swap.initiate_block_number` > 0
  - `source_swap.amount === source_swap.filled_amount`
  - `source_swap.current_confirmations >= source_swap.required_confirmations`
- Selects native vs SPL by checking `htlc_address` against `nativeSolanaAddress` / `splSolanaAddress`
- Native: calls `SolanaService.nativeSolanaInitiate(swapId, redeemer, secretHash, amountBN, timelockBN)`
- SPL: calls `SolanaService.splInitiate(timelock, redeemer, secretHash, swapAmount, initiator, mint)`

**executor/blockchain/solanaService.ts — `nativeSolanaInitiate()`**
- Builds tx with `nativeSolanaHTLC.methods.initiate(amount, expiresIn, redeemer, secretHash)`
- `initiator` account = `this.filler.publicKey` (the executor's keypair)
- Gets blockhash at `"confirmed"` commitment
- Signs as `VersionedTransaction`
- Calls `confirmTransaction()` after send

**executor/blockchain/solanaService.ts — `splInitiate()`**
- Resolves `tokenProgramId` via `TokenProgramIdResolver`
- Computes `initiatorTokenAccount` = ATA of `initiator` for the mint
- `sponsor` = `this.filler.publicKey` (executor pays rent)
- `initiator` = order's `destination_swap.initiator` (user)

### Order Redemption

**executor/executorUtils/orderHandler.ts — `shouldRedeem(order)` → `handleRedeem(order)`**
- Triggered when: `source_swap.chain.includes("solana")` AND `source_swap.initiate_tx_hash` non-empty AND `destination_swap.secret` present AND `source_swap.redeem_tx_hash` empty
- Secret comes from `order.destination_swap.secret` (revealed by dest chain after dest redeem)
- Native: `nativeSolanaRedeem(secret, secretHash, initiator, redeemer)`
  - PDA derived: `["swap_account", initiator, secretHash]`
- SPL: `splRedeem(secret, secretHash, initiator, redeemer, mint)`
  - PDA derived: `[initiator, secretHash]`
  - Fetches `swapData` to get `sponsor` address
  - Creates/gets redeemer ATA (executor pays for creation if needed)

**relayer/src/redeemer.ts — `Redeemer.run()`**
- Poll interval: configurable `poll_interval_secs` (default 5s)
- Filters: `destination_swap.chain.includes("solana")` AND `initiate_tx_hash != ""` AND both `redeem_tx_hash` and `refund_tx_hash` empty on both sides
- Fetches secrets from `credentials_url` by POSTing `{action: "retrieve", secret_hashes: [...]}`
- Calls `redeemAll()` concurrently via `Promise.allSettled`
- Caches successful swap IDs for 1 hour (prevents duplicate attempts)

### Refund / Instant Refund

**executor/executorUtils/orderHandler.ts — `shouldRefund(order, currentSlot)` → `handleRefund(order)`**
- Triggered when: `destination_swap.chain.includes("solana")` AND `initiate_tx_hash` non-empty AND `redeem_tx_hash` empty AND `currentSlot >= parseInt(initiate_block_number) + timelock` AND `refund_tx_hash` empty
- Native: `nativeSolanaRefund(secretHash, initiator)` — uses `filler.publicKey` as `initiator` in the call (executor initiated the dest swap)
- SPL: `splRefund(secretHash, initiator, mint)`

**executor/executorUtils/orderHandler.ts — `handleInstantRefund(order)`**
- Two triggers:
  1. `cobiAlreadyRefunded()`: source is Solana AND source `initiate_tx_hash` set AND dest `refund_tx_hash` set AND source `refund_tx_hash` empty
  2. `basicInstantRefundChecks()` AND `isDeadlineExpired(order, 30 * 60)`: source is Solana, source initiated, no dest initiate, deadline + 30min buffer exceeded
- Requires redeemer signature on-chain → executor signs as `redeemer: this.filler.publicKey`

**relayer/src/refunder.ts — `Refunder.run()`**
- Poll interval: configurable `poll_interval_secs` (default 10s)
- `filterRefundable()`: source is Solana AND source `initiate_tx_hash` set AND both `redeem_tx_hash` and `refund_tx_hash` empty AND `currentSlot >= initiate_block_number + timelock`
- Uses `this.solana.provider.connection.getSlot("confirmed")` for current slot

### Transaction Confirmation

**executor/blockchain/txConfirmation.ts — `confirmTransaction()`** (identical pattern in relayer/src/utils.ts)
- Poll interval: 400ms (`POLL_INTERVAL_MS = 400`)
- Default commitment: `"confirmed"`
- Loops calling `getSignatureStatus` + `getBlockHeight` in parallel
- Throws `"Transaction {sig} failed on-chain: {err}"` on on-chain error
- Throws `"Transaction {sig} expired: block height {h} exceeded lastValidBlockHeight {lv}"` on expiry

### PDA Derivation

**Native swap PDA (executor/blockchain/solanaService.ts — `getPda()`)**:
```
seeds = [Buffer.from("swap_account"), initiator.toBuffer(), secretHash]
programId = nativeHTLC.programId
```
- Used for: initiate (auto-derived by Anchor), redeem, refund, instantRefund

**SPL swap_data PDA** (executor and relayer):
```
seeds = [initiator.toBuffer(), secretHash]
programId = splHTLC.programId
```

**SPL token_vault PDA**:
```
seeds = [mint.toBuffer()]
programId = splHTLC.programId
```

**SPL identity_pda** (vault authority):
```
seeds = []
programId = splHTLC.programId
```

### Validator / Cross-Chain Verification

**executor/executorUtils/validatorService.ts — `ValidatorService.isValid(chainName, ValidationType.SourceInitiate, order)`**
- URL: `POST {executor_registry_url}/{chainName}/validate`
- Timeout: 5000ms
- Returns `false` on timeout or non-200 response
- Called before every destination initiate to ensure source chain executor has confirmed the source initiate

**executor/server/validator.ts — `validateSwap(swap, solanaService)`**
- Fetches on-chain PDA data
- Validates: amount match, expiry slot = `initiate_block_number + timelock`, initiator match, redeemer match
- Returns `{valid: false, error: "Swap not found..."}` if PDA does not exist

### Price Protection

**executor/executorUtils/orderHandler.ts — `hasPriceProtectionFailed(order)`**
- Threshold: `price_threshold` from config (default `0.01` = 1%)
- Skips if price ratio within 0.5% of 1.0 (likewise assets, e.g. stablecoin pairs)
- `combinedSystemLoss = inputPriceDecrease + outputPriceIncrease`
- Fails if `lossPercent > thresholdPercent` OR current output price < `originalOutputPrice * (1 - threshold)`
- Returns `true` (protection failed) if either price is 0

---

## 4. Known Failure Patterns

### 1. Blockhash Expiry
- **Symptom**: `Transaction {sig} expired: block height {h} exceeded lastValidBlockHeight {lv}`
- **Root cause**: Solana transactions have a ~150-block (~60s) validity window. If the network is slow or submission is retried too late, the blockhash is no longer valid.
- **Trigger**: High network congestion, slow RPC, or the `confirmTransaction` poll loop running but the tx was never propagated.
- **Where thrown**: `executor/blockchain/txConfirmation.ts:62` and `relayer/src/utils.ts:51`
- **Recovery**: Executor will retry on the next poll cycle (the actionsCache prevents double-submission for 10 min, but a restart clears it). The transaction simply needs to be re-sent with a fresh blockhash.

### 2. On-Chain Transaction Error
- **Symptom**: `Transaction {sig} failed on-chain: {err_json}`
- **Root cause**: The Solana program rejected the instruction (e.g., wrong secret, refund before expiry, wrong initiator)
- **Specific program errors** (from `SwapError` enum):
  - `InvalidSecret`: SHA256(provided_secret) != stored secret_hash
  - `RefundBeforeExpiry`: `current_slot <= expiry_slot` at time of refund attempt
  - `InvalidRedeemer`: redeemer address doesn't match on-chain record
  - `InvalidInitiator`: initiator address doesn't match
  - `InvalidRentPayer` / `InvalidSponsor`: rent payer/sponsor address mismatch
  - `RedeemerSameAsInitiator`: sanity check on initiate
  - `ZeroSwapAmount` / `ZeroExpiresInSlots`: sanity checks on initiate
- **Where thrown**: `executor/blockchain/txConfirmation.ts:39` and `relayer/src/utils.ts:37`

### 3. Swap Account Not Found
- **Symptom**: `Error fetching native swap data: ...` or `Error fetching SPL swap data: ...` (from `fetchNativeSwapAccount` / `fetchSplSwapAccount`) or `Swap not found for swap_id: ... and asset: ...`
- **Root cause**: PDA was never created (initiate failed or was not yet picked up), or was already closed (already redeemed/refunded).
- **Impact**: Validator returns `{valid: false}`, blocking dest initiation; or redeem/refund fails because account is already closed.
- **On-chain check**: Verify PDA existence at derived address.

### 4. Watcher Fails to Map Event to Order
- **Symptom**: `could not map on-chain event to an existing order` (watcher/src/storage.ts)
- **Root cause**: The on-chain event parameters (amount, timelock, initiator, redeemer, secret_hash, mint/token_address, htlc_address) do not match any row in the `swaps` table with an empty `initiate_tx_hash`.
- **Common causes**: Order not yet created in DB, amount mismatch (decimal confusion), token_address field mismatch ("primary" vs actual mint), or htlc_address mismatch (program ID mismatch).
- **DB query** (updateInit): matches on `amount`, `timelock`, `initiator`, `redeemer`, `secret_hash`, `token_address`, `htlc_address` AND `initiate_tx_hash = ''`.

### 5. Price Protection Failure (Executor Skips Initiate)
- **Symptom**: `Price protection failed for order: {orderId}` (info log)
- **Root cause**: Token prices moved > 1% (default threshold) unfavorably since order creation.
- **Recovery**: Automatic — executor re-evaluates on next poll cycle. If prices recover, initiation proceeds.

### 6. Validator Rejection (Executor Skips Initiate)
- **Symptom**: `solana-executor: Validator rejected Initiate action, skipping` (warn log)
- **Root cause**: The source chain executor returned non-200 to the validate endpoint — source initiate not yet confirmed on-chain, or source chain executor is down.
- **Recovery**: Automatic — executor retries on next poll cycle. If source executor is offline, initiation is blocked indefinitely.

### 7. Blacklisted Order
- **Symptom**: `Order {orderId} is blacklisted, skipping processing` (info log)
- **Root cause**: `order.create_order.additional_data.is_blacklisted` is not explicitly `false` (null, undefined, or true all count as blacklisted).

### 8. MintDoesNotExist Error (SPL)
- **Symptom**: `the provided mint account does not exist` (`MintDoesNotExist` error from `TokenProgramIdResolver`)
- **Root cause**: The `token_address` in the order points to a non-existent or invalid Solana account.

### 9. Keystore Unlock Failure
- **Symptom**: `Failed to unlock keystore` (executor startup)
- **Root cause**: Wrong password, keystore file missing, or max 3 attempts exceeded. Executor exits immediately.

### 10. Executor Registry Registration Failure
- **Symptom**: `Failed to register executor, exiting...` followed by `process.exit(1)`
- **Root cause**: 20 registration attempts (exponential backoff from 1s) all failed.

### 11. Watcher Transaction Not Found
- **Symptom**: `transaction not found: {signature}` (watcher/src/solana-client.ts)
- **Root cause**: RPC returned null for a known signature — typically a slot lag or archival node issue.
- **Impact**: `processFrom()` returns `undefined`, watcher skips the batch and retries from the same cursor next poll.

### 12. Watcher Block Time Missing
- **Symptom**: `could not find block time for transaction` or `error while fetching block time`
- **Root cause**: RPC returned null blockTime for slot; follow-up `getBlockTime(slot)` also failed.

### 13. Insufficient Lamports for Rent (SPL Initiate)
- **Symptom**: On-chain error during SPL initiate — `AccountNotEnoughKeys` or insufficient lamports
- **Root cause**: The `sponsor` (executor's filler wallet) does not have enough SOL to pay rent for `identity_pda`, `token_vault`, and `swap_data` PDAs.
- **Check**: Executor filler wallet balance on-chain.

### 14. TokenAccountNotFoundError During SPL Redeem
- **Symptom**: Caught in `getOrCreateAssociatedTokenAccount`, handled gracefully by creating ATA
- **Root cause**: Redeemer's ATA for the mint doesn't exist yet.
- **Recovery**: Relayer creates it automatically, paying the ATA creation fee.

---

## 5. Important Constants and Thresholds

### Executor

| Constant | Value | Location |
|----------|-------|----------|
| `executor_sleep_duration` default | 5000ms (5s) | `executor/config/configLoader.ts` |
| `executor_sleep_duration` min/max | 1000ms / 30000ms | `executor/config/configLoader.ts` |
| `price_threshold` default | 0.01 (1%) | `executor/config/configLoader.ts` |
| likewise-asset skip threshold | 0.005 (0.5% price ratio from 1.0) | `executor/executorUtils/orderHandler.ts:163` |
| `DEADLINE_BUFFER` | 30 * 60 = 1800 seconds | `executor/executorUtils/orderHandler.ts:16` |
| Validator timeout | 5000ms | `executor/executorUtils/validatorService.ts:23` |
| Quote service cache duration | 10000ms (10s) | `executor/blockchain/quoteService.ts:22` |
| Quote service request timeout | 5000ms | `executor/blockchain/quoteService.ts:44` |
| ActionsCache TTL | 10 * 60 * 1000ms (10 min) per action | `executor/executorUtils/actionsCache.ts:11-14` |
| Tx confirmation poll interval | 400ms | `executor/blockchain/txConfirmation.ts:4` |
| Default commitment for blockhash fetch | `"confirmed"` | `executor/blockchain/solanaService.ts:44` |
| Executor registration max attempts | 20 | `executor/utils/register.ts:120` |
| Executor registration initial backoff | 1000ms | `executor/utils/register.ts:121` |
| Registration request timeout | 30000ms | `executor/utils/register.ts:89` |
| Server default port | 8080 | `executor/config/configLoader.ts:25` |
| `HOST_ADDR` env var default | `http://0.0.0.0:4425` | `executor/main.ts:31` |

### Chain IDs (executor/utils/register.ts)
| chain_id | Chain name |
|----------|-----------|
| 101 | `"solana"` (mainnet) |
| 103 | `"solana_testnet"` |
| 104 | `"solana_localnet"` |

### Environment → Chain Identifier (executor/blockchain/solanaService.ts)
| environment | getSolanaChainIdentifier() |
|-------------|--------------------------|
| `"localnet"` or `"merry"` | `"solana_localnet"` |
| `"testnet"` | `"solana_testnet"` |
| `"mainnet"` | `"solana"` |

### Relayer

| Constant | Value | Location |
|----------|-------|----------|
| Default listen port | 5014 | `relayer/relayer-config.example.toml` |
| `auto_redeem.poll_interval_secs` default | 5s | `relayer/src/redeemer.ts:43` |
| `auto_refund.poll_interval_secs` default | 10s | `relayer/src/refunder.ts:27` |
| Redeemer/Refunder cache TTL | 3600s (1 hour) | `relayer/src/redeemer.ts:11`, `relayer/src/refunder.ts:12` |
| DB max connections | 150 | `relayer/src/storage.ts:5` |
| DB connect timeout | 30s | `relayer/src/storage.ts:6` |
| DB idle timeout | 10 min | `relayer/src/storage.ts:7` |
| DB max lifetime | 30 min | `relayer/src/storage.ts:8` |
| Tx confirmation poll interval | 400ms | `relayer/src/utils.ts:4` |
| Default commitment for tx confirmation | `"confirmed"` | `relayer/src/utils.ts:26` |
| SPL_INITIATE_INSTRUCTION_INITIATOR_INDEX | 3 | `relayer/src/solana-spl.ts:16` |
| SPL_INITIATE_INSTRUCTION_MINT_INDEX | 5 | `relayer/src/solana-spl.ts:17` |
| ATA creation confirmation poll | 500ms | `relayer/src/solana-spl.ts:326` |

### Watcher

| Constant | Value | Location |
|----------|-------|----------|
| `watcher_poll_interval_secs` example | 3s | `watcher/watcher-config.example.toml` |
| `confirmations_poll_interval_secs` example | 2s | `watcher/watcher-config.example.toml` |
| Default initial fetch limit (no cursor) | 100 transactions | `watcher/src/solana-client.ts` (uses `100` when `from` is undefined) |
| RPC_CHUNK_SIZE | 256 | `watcher/src/solana-client.ts:19` |
| RPC_WAIT_TIME_MILLIS | 1000ms | `watcher/src/solana-client.ts:21` |
| Inter-page fetch delay | 100ms | `watcher/src/solana-client.ts:66` |
| Confirmation threshold for "unconfirmed" | < 2 | `watcher/src/storage.ts:79` |
| Confirmation integer mapping | processed=0, confirmed=1, finalized=2 | `watcher/src/solana-client.ts:165-175` |
| Watcher DB max connections | 500 | `watcher/src/storage.ts:33` |

### On-Chain Programs

| Constant | Value | Location |
|----------|-------|----------|
| Native program ID (canonical) | `6eksgdCnSjUaGQWZ6iYvauv1qzvYPF33RTGTM1ZuyENx` | `native-swaps/programs/solana-native-swaps/src/lib.rs:3` |
| SPL program ID (canonical) | `2WXpY8havGjfRxme9LUxtjFHTh1EfU3ur4v6wiK4KdNC` | `spl-swaps/programs/solana-spl-swaps/src/lib.rs:7` |
| ANCHOR_DISCRIMINATOR | 8 bytes | both lib.rs files |
| Native SwapAccount INIT_SPACE | 8 + size_of(SwapAccount) = 8+8+8+32+32+32+1+32 = 153 bytes | computed from fields |
| Slot duration | 400ms (1 slot) | documented in program comments |
| Refund condition | `current_slot > expiry_slot` (strictly greater) | both lib.rs `refund()` |
| Instant refund condition | No slot check (redeemer signs) | both lib.rs `instant_refund()` |

---

## 6. Log Signatures

### Executor Logs

| Log Message | Level | Meaning |
|-------------|-------|---------|
| `Starting HTTP server...` | info | Startup phase 1 |
| `HTTP server started successfully` | info | Server ready |
| `Registering executor with registry...` | info | Startup phase 2 |
| `Executor registered successfully` | info | Registration done |
| `Starting executor order processing...` | info | Main loop starting |
| `Filler public key: {pubkey}` | info | Executor wallet identity |
| `Graceful shutdown initiated, stopping order processing` | info | SIGTERM/SIGINT received |
| `Order {orderId} is blacklisted, skipping processing` | info | Blacklisted skip (is_blacklisted != false) |
| `Price protection failed for order: {orderId}` | info | Price moved unfavorably |
| `solana-executor: Validator rejected Initiate action, skipping` | warn | Source chain validator returned non-200 |
| `Initiating swap for order: {orderId}` | info | About to call nativeSolanaInitiate or splInitiate |
| `Successfully processed Initiation for: {orderId}` | info | Dest initiate on-chain confirmed |
| `~ SolanaService ~ Native Solana Initiate Signature: {sig}` | info | Raw tx sig after submission |
| `~ SolanaService ~ SPL Initiate Signature: {sig}` | info | SPL variant |
| `Error initiating Solana swap for order: {orderId}` | error | handleInitiate failed |
| `Native Solana Initiate Transaction Error: {message}` | error | solanaService level error |
| `SPL Initiate Transaction Error: {message}` | error | SPL variant |
| `Redeeming Solana swap for order: {orderId}` | info | handleRedeem starting |
| `Successfully processed Redeem for: {orderId}` | info | Redeem confirmed |
| `~ SolanaService ~ Native Solana Redeem Signature: {sig}` | info | Redeem tx sig |
| `~ SolanaService ~ SPL Redeem Signature: {sig}` | info | SPL variant |
| `Error redeeming Solana swap for order: {orderId}` | error | handleRedeem failed |
| `Native Solana Redeem Transaction Error: {message}` | error | solanaService level |
| `SPL Redeem Transaction Error: {message}` | error | SPL variant |
| `Refunding Solana swap for order: {orderId}` | info | handleRefund starting |
| `Successfully processed Refund for: {orderId}` | info | Refund confirmed |
| `~ SolanaService ~ Native Solana Refund Signature: {sig}` | info | Refund tx sig |
| `Error refunding Solana swap for order: {orderId}` | error | handleRefund failed |
| `Native Solana Refund Transaction Error: {message}` | error | solanaService level |
| `Instant refund triggered for Solana swap for order: {orderId}` | info | handleInstantRefund starting |
| `Successfully processed Instant Refund for: {orderId}` | info | Instant refund confirmed |
| `~ SolanaService ~ Native Solana Instant Refund Signature: {sig}` | info | Instant refund tx sig |
| `Error performing instant refund for Solana swap for order: {orderId}` | error | Failed |
| `Order {orderId} detected, waiting for future processing.` | info | Order seen but no action taken this cycle |
| `Awaiting confirmation for tx: {sig} (commitment: confirmed)` | info | Polling started |
| `Transaction {sig} confirmed (status: {status})` | info | Confirmation done |
| `Transaction {sig} failed on-chain: {err}` | error (thrown) | On-chain error — FATAL for this order |
| `Transaction {sig} expired: block height {h} exceeded lastValidBlockHeight {lv}` | error (thrown) | Blockhash expired — transient, will retry |
| `pda generation error` | error | PDA derivation failed |
| `Solana Connection Error` | error | getSlot() RPC call failed |
| `Error fetching native swap data:` | error | fetchNativeSwapAccount failed |
| `Error fetching SPL swap data:` | error | fetchSplSwapAccount failed |
| `Failed to unlock keystore` | error | Startup failure — fatal |
| `Validating swap: {swapId} on chain: {chain}` | info | Validate endpoint called |
| `Swap not found for swap_id: {id} and asset: {asset}` | warn (returned) | PDA not on-chain |
| `Amount mismatch for swap {swapId}: expected {x}, got {y}` | warn | Validation mismatch |
| `Expiry slot mismatch for swap {swapId}: ...` | warn | Slot arithmetic mismatch |
| `Initiator mismatch for swap {swapId}: ...` | warn | Address mismatch |
| `Redeemer mismatch for swap {swapId}: ...` | warn | Address mismatch |
| `Swap {swapId} validated successfully` | info | Validation OK |
| `Validation successful` | info | ValidatorService call returned 200 |
| `Validation failed` | warn | ValidatorService call returned non-200 |
| `Validation request timed out` | error | 5s timeout hit |
| `Validation request failed` | error | Network error |
| `Sending validation request` | info | About to call validator |
| `Registration attempt failed, will retry` | warn | Registry registration failed, will backoff |
| `Executor registered successfully` | info | Registration complete |
| `Failed to register executor after retries` | error | Fatal after 20 attempts |
| `Processing order failed` | error | OrderHandler.processOrder threw |
| `Critical error in order processing` | error | getPendingOrders threw |
| `Solver Pending Orders API returned error status: {status}` | error | API returned non-Ok |
| `Solver Pending Orders API request failed: {msg}` | error | HTTP error fetching orders |
| `Quote Service API request failed: {msg}` | error | Quote server unreachable |

### Relayer Logs

| Log Message | Level | Meaning |
|-------------|-------|---------|
| `relayer public key: {pubkey}` | info | Startup |
| `relayer started on port {port}` | info | HTTP server ready |
| `auto redeem service started` | info | Redeemer loop starting |
| `auto refund service started` | info | Refunder loop starting |
| `serving redeem request` | info | `POST /redeem` received |
| `redeem transaction successfully relayed` | info | Redeem done |
| `could not relay redeem request` | error | Redeem failed |
| `error while auto-redeeming` | error | Auto-redeem failed for a swap |
| `successfully auto-redeemed` | info | Auto-redeem succeeded |
| `error while auto-refunding` | error | Auto-refund failed |
| `successfully auto-refunded` | info | Auto-refund succeeded |
| `could not fetch solana orders from pending orders endpoint` | error | Transient: network error |
| `error fetching solana orders` | error | Transient: HTTP error |
| `could not fetch secrets` | error | Credentials endpoint unreachable |
| `got error from pending orders endpoint` | error | API-level error |
| `got error from secrets endpoint` | error | Credentials API error |
| `invalid json output from pending orders endpoint` | error | Malformed response |
| `either of initiator, redeemer or token_address is in invalid format` | error | Bad pubkey in order |
| `invalid initiator or token_address format in refundable order` | error | Bad pubkey in order |
| `invalid request format` | error | HTTP client sent bad body |
| `Transaction failed: {err}` | thrown | On-chain error |
| `Transaction {sig} expired: ...` | thrown | Blockhash expiry |
| `error in auto refund loop` | error | Refunder outer loop caught exception |

### Watcher Logs

| Log Message | Level | Meaning |
|-------------|-------|---------|
| `starting watcher` | info | Startup with program and poll interval |
| `Processing successful` | info | Batch of txns processed; shows from/till signatures and count |
| `error fetching transactions` | error | RPC call failed; watcher retries next poll |
| `error parsing event` | error | Anchor EventParser failed; watcher returns undefined for batch |
| `error while fetching block time` | error | RPC getBlockTime failed |
| `could not find block time for transaction` | error | Null blockTime even after retry |
| `database error while updating events` | error | DB write failed |
| `successfully processed on-chain event` | info | Event mapped to swap_id in DB |
| `could not map on-chain event to an existing order` | warn | No matching DB row — data mismatch or race condition |
| `skipping failed transaction` | warn | On-chain tx was failed, skipped |
| `logs not found for transaction: {sig}` | thrown | Missing logMessages in RPC response |
| `transaction not found: {sig}` | thrown | RPC returned null for signature |
| `invalid txn` | error | getSignatureStatuses returned null for a sig |
| `could not fetch confirmation status for txn` | error | confirmationStatus is null |
| `confirmations updated successfully` | info | Confirmation loop finished a batch |
| `error fetching unconfirmed transactions` | error | DB query failed |
| `error fetching confirmations` | error | RPC getSignatureStatuses failed |
| `error updating confirmations` | error | DB update failed |

### Transient vs Fatal

**Transient (will auto-recover)**:
- Blockhash expiry — executor retries on next poll
- Price protection failure — retried next poll
- Validator rejection — retried next poll
- RPC errors (connection, timeout) — retried next poll
- DB connection errors — retried next poll
- `could not map on-chain event to existing order` (if order arrives in DB later)

**Fatal / Requires Investigation**:
- `Failed to unlock keystore` — executor never starts
- `Failed to register executor after retries` — executor exits
- `Transaction failed on-chain: InvalidSecret` — secret is wrong, order is stuck
- `Transaction failed on-chain: InvalidRedeemer/Initiator` — address mismatch, order is stuck
- `could not map on-chain event to existing order` (persistent) — DB schema mismatch or wrong program ID
- Consistently zero orders from pending orders API — pipeline broken upstream

---

## 7. On-Chain Checks Per Failure Type

### missed_init (Solana destination swap never initiated)

**Hypothesis A: Executor never attempted**
- Check: No `"Initiating swap for order: {orderId}"` log in executor. Check `basicInitiateChecks` conditions:
  - Is `source_swap.initiate_tx_hash` populated in DB?
  - Is `source_swap.current_confirmations >= required_confirmations`?
  - Is `source_swap.amount == source_swap.filled_amount`?
  - Is `initiate_block_number > 0`?

**Hypothesis B: Price protection blocking**
- Check: Look for `"Price protection failed for order: {orderId}"` in executor logs.
- Action: Verify current prices via quote server `GET {quote_server_url}/fiat?order_pair=...`.

**Hypothesis C: Validator blocking**
- Check: Look for `"Validator rejected Initiate action, skipping"` in executor logs.
- Action: POST to source chain executor's `/validate` endpoint manually; check if source chain executor is healthy.

**Hypothesis D: Deadline exceeded**
- Check: `isDeadlineExpired()` — `currentTimestamp > order.create_order.additional_data.deadline`.
- On-chain: Cannot initiate if deadline has passed.

**Hypothesis E: Initiation attempted but failed on-chain**
- Check: Look for `"Error initiating Solana swap for order: {orderId}"` and `"Native Solana Initiate Transaction Error"` in executor logs.
- On-chain: Verify PDA does NOT exist — `getAccountInfo(PDA_address)` should return null if init failed/never happened.
- Native PDA: `findProgramAddress(["swap_account", initiator, secretHash], nativeProgramId)`
- SPL swap_data PDA: `findProgramAddress([initiator, secretHash], splProgramId)`

**Hypothesis F: Transaction expired before confirmation**
- Check: Look for `"Transaction {sig} expired: block height {h} exceeded lastValidBlockHeight {lv}"` in executor logs.
- On-chain: The transaction was never included — PDA does not exist. Executor will retry on next cycle.

### deadline_approaching / deadline_exceeded

- Check DB: `order.create_order.additional_data.deadline` vs current timestamp.
- Check if dest initiate exists: `destination_swap.initiate_tx_hash` in DB or PDA on-chain.
- If no dest initiate and deadline near: executor should be attempting but may be blocked by price/validator.
- If deadline passed with no dest initiate: instant refund eligibility depends on whether `basicInstantRefundChecks` passes.

### stuck_redeem (Source Solana swap initiated, secret revealed, no redeem)

**Hypothesis A: Executor has not processed yet**
- Check: Is `order.destination_swap.secret` populated (visible to executor)?
- On-chain: PDA at native `["swap_account", initiator, secretHash]` or SPL `[initiator, secretHash]` — if it exists, redeem has NOT happened yet.

**Hypothesis B: Redeem failed on-chain**
- Check executor logs for `"Error redeeming Solana swap for order: {orderId}"` or `"Native Solana Redeem Transaction Error"`.
- Common cause: `InvalidSecret` — the secret `SHA256(s)` does not match `secret_hash` stored in `SwapAccount`.
- On-chain: Fetch PDA via `program.account.swapAccount.fetch(pdaAddress)`, inspect `secret_hash` field.
- Verify: `SHA256(hex_decode(order.destination_swap.secret))` should equal `swap_account.secret_hash`.

**Hypothesis C: Actionsache preventing retry**
- Check: If executor was restarted within 10 minutes of a successful (but not DB-confirmed) redeem attempt, the cache is clear. If the actionsCache entry for REDEEM is set, executor won't retry until 10 min TTL expires.
- Resolution: Wait 10 minutes or restart executor.

**Hypothesis D: Redeemer ATA missing (SPL only)**
- On-chain: Check if redeemer has an ATA for the mint. Relayer's `getOrCreateAssociatedTokenAccount` handles this, but executor's `splRedeem` also tries to get the ATA directly without creating it.
- Resolution: Ensure redeemer ATA exists, or use relayer's redeem endpoint.

### stuck_refund (Timelock expired, no refund)

**Check timelock expiry**:
- DB: `initiate_block_number + timelock` = expiry slot.
- On-chain: `getSlot("confirmed")` — if `currentSlot > expirySlot`, refund is eligible.
- Verify by fetching PDA: `swapAccount.expiry_slot` must be < `currentSlot`.

**Hypothesis A: Executor not attempting**
- Check `shouldRefund()` conditions: dest chain = solana, `initiate_tx_hash` set, no redeem, `currentSlot >= initiate_block_number + timelock`, no refund.
- Verify current slot from RPC vs on-chain expiry slot.

**Hypothesis B: Refund failed — RefundBeforeExpiry**
- Check logs for `"Transaction {sig} failed on-chain: {...}"` with `RefundBeforeExpiry`.
- On-chain: `swapAccount.expiry_slot` > current slot — the timelock slot has not been reached yet.
- Resolution: Wait until the expiry slot is passed.

**Hypothesis C: Wrong initiator passed**
- Native `handleRefund` uses `order.destination_swap.initiator` for the account constraint, but the transaction uses `this.filler.publicKey` as the initiator signer argument. These must match.
- On-chain: `swapAccount.initiator` should equal the executor's filler public key (since executor initiated the dest swap).

### stuck_instant_refund

**On-chain verification**:
- PDA must exist (not already refunded/redeemed).
- For native: PDA at `["swap_account", initiator, secretHash]`.
- For SPL: PDA at `[initiator, secretHash]`.
- Instant refund requires redeemer's signature — executor (filler) must be the redeemer.
- Check: `swapAccount.redeemer == executor_filler_pubkey`.

**Hypothesis: Already closed**
- `getAccountInfo(PDA)` returns null → swap is already settled (redeemed, refunded, or instant-refunded).
- Check DB for `refund_tx_hash` or `redeem_tx_hash` being set.

### watcher_stuck (Events not reaching DB)

**Check watcher health**:
- Look for `"Processing successful"` logs — if absent, watcher is stalled.
- Check `"error fetching transactions"` — RPC is down.
- Check `"database error while updating events"` — DB is unreachable.

**Check cursor/bookmark**:
- Watcher tracks `from` (last processed signature). If this is wrong (e.g., a signature that doesn't exist), `getSignaturesForAddress(programId, {until: badSig})` may return empty.
- Resolution: Update `start_after_transaction` in config to a known-good recent signature, or clear it to process last 100.

**On-chain event mapping failure**:
- If watcher is processing but not mapping, check the SQL WHERE conditions in `updateInit`:
  - `sw.amount = input_data.swap_amount`
  - `sw.timelock = input_data.expires_in_slots`
  - `sw.initiator = input_data.initiator`
  - `sw.redeemer = input_data.redeemer`
  - `sw.secret_hash = input_data.secret_hash`
  - `sw.token_address = input_data.mint` (must be `"primary"` for native, or mint pubkey for SPL)
  - `sw.htlc_address = input_data.program_id` (must match the program ID in watcher config)
  - `sw.initiate_tx_hash = ''` (must not already have been mapped)

**Confirmation loop stuck**:
- Query DB: `SELECT initiate_tx_hash FROM swaps WHERE COALESCE(current_confirmations,0) < 2 AND initiate_tx_hash != '' AND chain LIKE '%solana%'`
- If many rows here and not advancing, check `getSignatureStatuses` RPC call health.

### Solana-Specific: Account State Inspection

**Fetch native SwapAccount PDA**:
```
PDA = findProgramAddressSync(
  [Buffer.from("swap_account"), initiator.toBuffer(), secretHash],
  new PublicKey("6eksgdCnSjUaGQWZ6iYvauv1qzvYPF33RTGTM1ZuyENx")
)
```
Fields to check: `amount_lamports`, `expiry_slot`, `initiator`, `redeemer`, `secret_hash`, `rent_payer`

**Fetch SPL swap_data PDA**:
```
PDA = findProgramAddressSync(
  [initiator.toBuffer(), secretHash],
  new PublicKey("2WXpY8havGjfRxme9LUxtjFHTh1EfU3ur4v6wiK4KdNC")
)
```
Fields to check: `mint`, `expiry_slot`, `initiator`, `redeemer`, `secret_hash`, `swap_amount`, `sponsor`

**If PDA does not exist (getAccountInfo returns null)**:
- Initiate has not happened yet OR swap is already settled (closed on redeem/refund)
- Cross-check with DB: if `initiate_tx_hash` is set in DB but PDA is null → swap was settled on-chain but DB may not have the redeem/refund event yet (watcher lag).

**If PDA exists**:
- Compare on-chain `expiry_slot` vs current slot → determines refund eligibility
- Compare on-chain `amount_lamports` (native) or `swap_amount` (SPL) vs DB amount → detect amount mismatch
- Check if `secret_hash` on-chain matches DB `secret_hash` → detect hash mismatch

**SPL token_vault PDA**:
```
vault = findProgramAddressSync(
  [mint.toBuffer()],
  new PublicKey("2WXpY8havGjfRxme9LUxtjFHTh1EfU3ur4v6wiK4KdNC")
)
```
- Check token balance: should equal `swap_amount` if active, 0 if settled.

**Executor filler wallet balance check**:
- If filler balance is low: SPL initiations fail (can't pay rent for PDAs), ATA creations fail.
- Minimum required per SPL initiate: ~0.003 SOL for `swap_data` rent + `identity_pda`/`token_vault` if first init.
