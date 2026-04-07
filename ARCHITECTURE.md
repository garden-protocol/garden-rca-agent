# Garden RCA Agent — Architecture

> Automated Root Cause Analysis for Garden cross-chain bridge alerts.
> When an alert fires, this system replaces the on-call debug loop with a pipeline of
> specialized AI agents that query logs, inspect on-chain state, read source code,
> and return a structured RCA report in seconds.

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Agent Hierarchy](#2-agent-hierarchy)
3. [RCA Request Flow](#3-rca-request-flow)
4. [Study Mode Flow](#4-study-mode-flow)
5. [Agent Detail — Internals](#5-agent-detail--internals)
6. [Data Flow — What Each Agent Receives and Returns](#6-data-flow--what-each-agent-receives-and-returns)
7. [File Structure](#7-file-structure)
8. [Configuration & Environment](#8-configuration--environment)
9. [How to Train the Agents](#9-how-to-train-the-agents)
10. [Adding a New Chain](#10-adding-a-new-chain)

---

## 1. System Overview

```mermaid
graph TB
    subgraph External["External Triggers"]
        ALERT[Alert / Webhook<br/>POST /rca]
        STUDY_TRIGGER[Manual Trigger<br/>POST /study/{chain}]
    end

    subgraph API["FastAPI Server"]
        MAIN[main.py<br/>Route + Validate]
    end

    subgraph Pipeline["RCA Pipeline"]
        ORCH[Orchestrator<br/>agents/orchestrator.py]
        LOG[Log Intelligence Agent<br/>agents/log_agent.py]
        SPEC[Chain Specialist<br/>agents/specialists/{chain}.py]
        ONCHAIN[On-Chain Agent<br/>agents/onchain/{chain}.py]
    end

    subgraph DataSources["Data Sources"]
        LOKI[(Loki<br/>Log Storage)]
        RPC[(Chain RPC<br/>Bitcoin / EVM / Solana / Spark)]
        REPO[(Cloned Repos<br/>Local Filesystem)]
        KNOWLEDGE[(knowledge/{chain}.md<br/>Generated KT Docs)]
    end

    subgraph StudyPipeline["Study Mode"]
        STUDY[Study Agent<br/>study/study_agent.py]
    end

    subgraph Output["Output"]
        REPORT[RCAReport JSON<br/>root_cause · evidence · actions · severity]
        KT_DOC[knowledge/{chain}.md<br/>Committed to Git]
    end

    ALERT --> MAIN
    STUDY_TRIGGER --> MAIN
    MAIN --> ORCH
    MAIN --> STUDY

    ORCH --> LOG
    ORCH --> SPEC
    SPEC --> ONCHAIN

    LOG --> LOKI
    ONCHAIN --> RPC
    SPEC --> REPO
    SPEC --> KNOWLEDGE

    STUDY --> REPO
    STUDY --> KT_DOC

    ORCH --> REPORT
```

---

## 2. Agent Hierarchy

```mermaid
graph TD
    ORCH["🎯 Orchestrator<br/>(routes alert, assembles report)"]

    ORCH --> LOG_AGENT["📋 Log Intelligence Agent<br/>(queries Loki, builds timeline)"]
    ORCH --> SPEC_BTC["₿ Bitcoin Specialist<br/>(reads code, reasons about BTC)"]
    ORCH --> SPEC_EVM["⟠ EVM Specialist<br/>(reads code, reasons about EVM)"]
    ORCH --> SPEC_SOL["◎ Solana Specialist<br/>(reads code, reasons about Solana)"]
    ORCH --> SPEC_SPK["⚡ Spark Specialist<br/>(reads code, reasons about Spark)"]

    SPEC_BTC --> OC_BTC["🔗 Bitcoin On-Chain Agent<br/>(tx lookup, mempool, fee rate)"]
    SPEC_EVM --> OC_EVM["🔗 EVM On-Chain Agent<br/>(receipts, gas, contract state)"]
    SPEC_SOL --> OC_SOL["🔗 Solana On-Chain Agent<br/>(accounts, signatures, slots)"]
    SPEC_SPK --> OC_SPK["🔗 Spark On-Chain Agent<br/>(EVM-compat RPC queries)"]

    LOG_AGENT -.->|"log summary"| SPEC_BTC
    LOG_AGENT -.->|"log summary"| SPEC_EVM
    LOG_AGENT -.->|"log summary"| SPEC_SOL
    LOG_AGENT -.->|"log summary"| SPEC_SPK

    OC_BTC -.->|"on-chain findings"| SPEC_BTC
    OC_EVM -.->|"on-chain findings"| SPEC_EVM
    OC_SOL -.->|"on-chain findings"| SPEC_SOL
    OC_SPK -.->|"on-chain findings"| SPEC_SPK
```

Only the specialist matching the alert's `chain` field is activated per request.

---

## 3. RCA Request Flow

```mermaid
sequenceDiagram
    participant Client
    participant API as FastAPI (main.py)
    participant Orch as Orchestrator
    participant LogA as Log Intelligence Agent
    participant Loki
    participant Spec as Chain Specialist
    participant OnChain as On-Chain Agent
    participant RPC as Chain RPC
    participant Repo as Local Repo

    Client->>API: POST /rca (Alert JSON)
    API->>API: Validate alert (Pydantic)
    API->>Orch: orchestrator.run(alert)

    Note over Orch: Step 1 — Logs
    Orch->>LogA: log_agent.run(alert)
    LogA->>Loki: search_by_order_id(order_id)
    Loki-->>LogA: log lines
    LogA->>Loki: search_by_service(service, chain, network)
    Loki-->>LogA: log lines
    LogA->>LogA: Claude synthesizes log timeline + errors
    LogA-->>Orch: {summary: "...", raw_lines: [...]}

    Note over Orch: Step 2 — On-Chain
    Orch->>OnChain: agent.query(question, log_context)
    OnChain->>RPC: get_transaction / check_mempool / etc.
    RPC-->>OnChain: on-chain data
    OnChain->>OnChain: Claude synthesizes findings
    OnChain-->>Orch: {findings: "...", tool_calls: [...]}

    Note over Orch: Step 3 — Specialist Analysis
    Orch->>Spec: specialist.analyze(alert, log_summary, onchain_findings)
    Spec->>Spec: Load knowledge/{chain}.md (cached)
    Spec->>Repo: read_file / grep_repo / list_directory
    Repo-->>Spec: source code
    Spec->>Spec: Claude reasons about root cause
    Spec-->>Orch: {root_cause, severity, confidence, suggested_actions, ...}

    Note over Orch: Step 4 — Assemble Report
    Orch->>Orch: Build RCAReport
    Orch-->>API: RCAReport
    API-->>Client: 200 OK (RCAReport JSON)
```

---

## 4. Study Mode Flow

Triggered manually when source code changes. Generates `knowledge/{chain}.md` — the KT doc injected into the specialist's system prompt.

```mermaid
sequenceDiagram
    participant Dev as Developer
    participant API as FastAPI (main.py)
    participant Study as Study Agent
    participant Repo as Cloned Repo (local)
    participant Disk as knowledge/{chain}.md
    participant Git

    Dev->>API: POST /study/{chain}
    API->>Study: study_agent.run(chain)
    Study->>Repo: git pull --ff-only (refresh)
    Study->>Repo: list_directory("/")
    Repo-->>Study: project structure

    loop Read source files
        Study->>Repo: read_file(path)
        Repo-->>Study: file contents
        Study->>Repo: grep_repo(pattern)
        Repo-->>Study: matching lines
    end

    Study->>Study: Claude writes 7-section knowledge doc
    Study->>Disk: Write knowledge/{chain}.md
    Study-->>API: {status: ok, knowledge_file: "..."}
    API-->>Dev: {status, path, duration, next_step}

    Dev->>Git: git add knowledge/{chain}.md
    Dev->>Git: git commit -m "chore: update {chain} knowledge doc"
    Note over Git: All engineers benefit from the updated doc
```

### Knowledge Doc Structure (7 Sections)

| # | Section | Purpose |
|---|---------|---------|
| 1 | Service Architecture Overview | How executor/watcher/relayer interact, order lifecycle |
| 2 | Key Files and Their Roles | Entry points, core business logic, config |
| 3 | Critical Functions | initiate, redeem, refund, fee estimation, retry logic |
| 4 | Known Failure Patterns | Error messages, conditions that trigger each failure |
| 5 | Important Constants & Thresholds | Timeouts, fee floors, retry counts, magic numbers |
| 6 | Log Signatures | What log lines mean, transient vs. fatal errors |
| 7 | On-Chain Checks per Failure Type | What to verify on-chain for each alert_type |

---

## 5. Agent Detail — Internals

Every agent runs a **manual agentic loop** — no SDK auto-runner. This gives full control over error handling and graceful degradation.

```mermaid
flowchart LR
    subgraph AgentLoop["Agentic Loop (all agents)"]
        direction TB
        A[Build messages array] --> B[Call Claude API]
        B --> C{stop_reason?}
        C -->|end_turn| D[Extract text response]
        C -->|tool_use| E[Execute tool calls]
        E --> F[Append tool results to messages]
        F --> B
    end

    subgraph ClaudeConfig["Claude API Config"]
        M[Model: claude-opus-4-6]
        TH[Thinking: adaptive]
        EF[Effort: high / medium]
        CC[Cache: ephemeral 1h<br/>on knowledge docs]
    end

    ClaudeConfig --> AgentLoop
```

### Effort Levels by Agent

| Agent | Effort | Reasoning |
|-------|--------|-----------|
| Chain Specialist | `high` | Core diagnosis — needs deep reasoning |
| Orchestrator synthesis | `high` | Final assembly from multiple sources |
| Log Intelligence Agent | `medium` | Pattern matching, less complex |
| On-Chain Agent | `medium` | Tool execution + summarization |
| Study Agent | `high` | Deep code comprehension pass |

---

## 6. Data Flow — What Each Agent Receives and Returns

```mermaid
flowchart TD
    ALERT["Alert\n───────────────\norder_id\nalert_type\nchain · service · network\nmessage · timestamp\ndeadline · metadata"]

    ALERT --> LOG_IN["Log Agent INPUT\n───────────────\nFull Alert object"]
    LOG_IN --> LOKI_QUERY["Loki Queries\n───────────────\nsearch_by_order_id\nsearch_by_service\nquery_loki (raw LogQL)"]
    LOKI_QUERY --> LOG_OUT["Log Agent OUTPUT\n───────────────\nsummary: markdown report\nraw_lines: list[str] (max 500)"]

    ALERT --> OC_IN["On-Chain Agent INPUT\n───────────────\nQuestion (from alert)\nLog context (first 1500 chars)"]
    OC_IN --> RPC_CALLS["RPC Calls\n───────────────\nget_transaction\ncheck_mempool\nget_gas_price\netc."]
    RPC_CALLS --> OC_OUT["On-Chain Agent OUTPUT\n───────────────\nfindings: str\ntool_calls: list"]

    LOG_OUT --> SPEC_IN["Specialist INPUT\n───────────────\nFull Alert\nLog summary\nOn-chain findings\nknowledge/{chain}.md (cached)"]
    OC_OUT --> SPEC_IN

    SPEC_IN --> REPO_CALLS["Repo Tool Calls\n───────────────\nread_file\ngrep_repo\nlist_directory"]
    REPO_CALLS --> SPEC_OUT["Specialist OUTPUT\n───────────────\nroot_cause\naffected_components\nsuggested_actions\nseverity · confidence\nraw_analysis"]

    SPEC_OUT --> REPORT["RCAReport (final)\n───────────────\norder_id · chain · service · network\nroot_cause\naffected_components\nlog_evidence\nonchain_evidence\nsuggested_actions\nseverity · confidence\nraw_analysis\ngenerated_at · duration_seconds"]
```

---

## 7. File Structure

```
rca-agent/
├── main.py                          # FastAPI server, /rca and /study/{chain} endpoints
├── config.py                        # Pydantic Settings, loads from .env
├── requirements.txt
├── .env.example                     # Template for all required env vars
│
├── models/
│   ├── alert.py                     # Alert (input model)
│   └── report.py                    # RCAReport (output model)
│
├── tools/
│   ├── loki.py                      # Loki HTTP API client + 3 Claude tool definitions
│   └── repo.py                      # Repo reader (read_file, grep_repo, list_directory)
│
├── agents/
│   ├── orchestrator.py              # Routes alert → agents, assembles RCAReport
│   ├── log_agent.py                 # Log Intelligence Agent (Loki queries)
│   │
│   ├── specialists/
│   │   ├── base.py                  # BaseSpecialist (agentic loop + repo tools)
│   │   ├── bitcoin.py               # Bitcoin Specialist
│   │   ├── evm.py                   # EVM Specialist
│   │   ├── solana.py                # Solana Specialist
│   │   └── spark.py                 # Spark Specialist
│   │
│   └── onchain/
│       ├── base.py                  # BaseOnChainAgent (agentic loop)
│       ├── bitcoin.py               # Bitcoin RPC agent (5 tools)
│       ├── evm.py                   # EVM web3.py agent (5 tools)
│       ├── solana.py                # Solana JSON-RPC agent (4 tools)
│       └── spark.py                 # Spark JSON-RPC agent (3 tools)
│
├── study/
│   └── study_agent.py               # Reads repo → generates knowledge/{chain}.md
│
├── prompts/                         # Override specialist system prompts here
│   └── (empty — add {chain}_specialist.txt to override Python defaults)
│
├── knowledge/                       # Auto-generated by POST /study/{chain}
│   └── (populated after study mode runs — commit these to git)
│
└── incidents/                       # Historical incident patterns (feed later)
    └── (add {chain}.yaml as incidents accumulate)
```

---

## 8. Configuration & Environment

All configuration is in `.env` (see `.env.example`):

```mermaid
graph LR
    subgraph Required
        AKEY[ANTHROPIC_API_KEY]
        LOKI[LOKI_URL]
    end

    subgraph Optional_Infra["Optional (degrade gracefully if missing)"]
        LAUTH[LOKI_AUTH_TOKEN]
        GURL[GRAFANA_URL + GRAFANA_API_KEY]
    end

    subgraph Repos["Repo Paths (needed for specialist tool use)"]
        RB[REPO_BITCOIN]
        RE[REPO_EVM]
        RS[REPO_SOLANA]
        RSP[REPO_SPARK]
    end

    subgraph RPCs["RPC URLs (needed for on-chain agents)"]
        BRPC[BITCOIN_RPC_URL + USER + PASS]
        ERPC[EVM_RPC_URL]
        SRPC[SOLANA_RPC_URL]
        SPRPC[SPARK_RPC_URL]
    end
```

---

## 9. How to Train the Agents

The agents start with sensible baselines built into their Python code. To make them Garden-specific:

### Step 1 — Run Study Mode (per chain)

```bash
curl -X POST http://localhost:8000/study/bitcoin
curl -X POST http://localhost:8000/study/evm
curl -X POST http://localhost:8000/study/solana
curl -X POST http://localhost:8000/study/spark
```

This reads the cloned repo, generates `knowledge/{chain}.md`, and caches it in specialist prompts.

### Step 2 — Commit the Knowledge Docs

```bash
git add knowledge/
git commit -m "chore: initial chain knowledge docs from study mode"
git push
```

All engineers and the agent immediately benefit.

### Step 3 — Write Custom Specialist Prompts (optional, after enough incidents)

Create `prompts/{chain}_specialist.txt` with Garden-specific system prompt text.
The specialist will load this file in preference to its Python default.

### Step 4 — Feed Past Incidents

Add entries to `incidents/{chain}.yaml` and re-run study mode.
The study agent will read past incidents and focus on related code paths.

---

## 10. Adding a New Chain

1. **On-Chain Agent** — create `agents/onchain/{chain}.py` extending `BaseOnChainAgent`
   - Implement `chain`, `tool_definitions`, `execute_tool`
   - Add it to `_ONCHAIN_AGENTS` in `orchestrator.py`

2. **Specialist** — create `agents/specialists/{chain}.py` extending `BaseSpecialist`
   - Implement `chain`, `system_prompt` (or add a `prompts/{chain}_specialist.txt`)
   - Add it to `_SPECIALISTS` in `orchestrator.py`

3. **Config** — add `repo_{chain}` and `{chain}_rpc_url` to `config.py`

4. **Models** — add the new chain name to the `Literal` union in `models/alert.py`

5. **Study** — run `POST /study/{chain}` to generate the knowledge doc

6. **Endpoints** — add the chain to `SUPPORTED_CHAINS` in `main.py`

---

## API Reference

### `POST /rca`

Trigger a full RCA for an alert.

**Request body (`Alert`):**
```json
{
  "order_id": "ord_abc123",
  "alert_type": "deadline_approaching",
  "chain": "bitcoin",
  "service": "executor",
  "network": "mainnet",
  "message": "Order approaching deadline with no init on destination",
  "timestamp": "2026-04-07T10:00:00Z",
  "deadline": "2026-04-07T10:30:00Z",
  "metadata": {}
}
```

**Response (`RCAReport`):**
```json
{
  "order_id": "ord_abc123",
  "chain": "bitcoin",
  "service": "executor",
  "network": "mainnet",
  "root_cause": "Fee rate (1 sat/vbyte) was below mempool minimum (8 sat/vbyte) at broadcast time. Transaction never entered mempool.",
  "affected_components": ["fee_estimator", "broadcaster"],
  "log_evidence": ["fee_rate=1 mempool_min_fee=8", "broadcast: transaction rejected"],
  "onchain_evidence": {"findings": "Transaction hash not found in mempool or chain.", "tool_calls_count": 2},
  "suggested_actions": [
    "Raise the minimum fee floor in fee_estimator.go:45",
    "Implement dynamic fee multiplier based on mempool congestion",
    "Add mempool acceptance check before returning from broadcaster"
  ],
  "severity": "high",
  "confidence": "high",
  "raw_analysis": "...",
  "generated_at": "2026-04-07T10:00:45Z",
  "duration_seconds": 42.1
}
```

### `POST /study/{chain}`

Generate or refresh the knowledge doc for a chain.

**Response:**
```json
{
  "status": "ok",
  "chain": "bitcoin",
  "knowledge_file": "/opt/rca-agent/knowledge/bitcoin.md",
  "duration_seconds": 87.4,
  "next_step": "git add knowledge/bitcoin.md && git commit -m 'chore: update bitcoin knowledge doc'"
}
```

### `GET /health`

```json
{"status": "ok", "chains": ["bitcoin", "evm", "solana", "spark"]}
```
