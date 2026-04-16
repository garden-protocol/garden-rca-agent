# Report Shape Upgrade (Batch C)

**Date:** 2026-04-16
**Status:** Approved (auto mode), pending implementation
**Related review:** Gap #6 — "Report model is missing the fields on-call actually wants."

---

## Background

`RCAReport` today carries `root_cause`, `affected_components`, `investigation_summary`, `key_log_evidence`, `onchain_evidence`, `remediation_actions`, `severity`, `confidence`. Missing fields on-call actually reads first:
- **Timeline** — what happened when, in order.
- **Next action** — one imperative to run right now.
- **Hypotheses ruled out** — what is confirmed *not* the cause.
- **Links** — click-through to block explorers and source files.

## Goal

Extend the report with these four fields, render them in Discord, and auto-generate explorer / Gitea links from data already present so the specialist doesn't have to invent URLs.

## Non-goals

- Grafana log-board links (requires deployment-specific config; defer).
- Confidence reasoning field (can live inside investigation_summary for now).
- Changing severity/confidence enums.

---

## Design

### Change 1 — New Pydantic models

In `models/report.py`:

```python
class TimelineEvent(BaseModel):
    timestamp: str          # ISO8601 preferred; may be "t+30s" if relative
    event: str              # plain-English description
    source: str = ""        # "logs" | "onchain" | "alert" | "orderbook"

class ReportLink(BaseModel):
    label: str              # human-readable, e.g. "Source init tx" or "evm-executor/src/main.rs:L42"
    url: str                # full https URL
    kind: str               # "tx" | "code" | "address" | "order"
```

Extend `RCAReport`:

```python
class RCAReport(BaseModel):
    # ... existing fields ...
    timeline: list[TimelineEvent] = []
    hypotheses_ruled_out: list[str] = []
    next_action: str = ""
    links: list[ReportLink] = []
```

All four default to empty so existing clients aren't broken by missing data.

### Change 2 — Specialist JSON schema extension

Extend the JSON block the specialist emits (in `agents/specialists/base.py`):

```json
{
  "root_cause": "...",
  "affected_components": [...],
  "investigation_summary": "...",
  "remediation_actions": [...],
  "severity": "...",
  "confidence": "...",

  "timeline": [
    {"timestamp": "2026-04-10T12:00:00Z", "event": "User initiated on source", "source": "logs"},
    {"timestamp": "2026-04-10T14:00:00Z", "event": "Source timelock expired, refund fired", "source": "onchain"}
  ],
  "hypotheses_ruled_out": [
    "Not a liquidity issue — solver had sufficient inventory",
    "Not a blacklist — additional_data.is_blacklisted=false"
  ],
  "next_action": "Restart evm-executor on node X to flush stuck nonce"
}
```

`next_action` is **one sentence**, distinct from `remediation_actions` (which is a list of 2-5 items). Prompt guidance:

> `next_action` is the single most important step the on-call should take right now, in imperative voice. Distinct from remediation_actions which enumerates all possible steps.

`timeline` must have 3-8 entries; `hypotheses_ruled_out` must have 0-5 entries.

Parse the new fields in `analyze()` and return them in the result dict. Unknown / missing fields default to empty list or empty string. Tolerate both `timestamp: <ISO>` and dict-shaped entries from models that return minor shape variants.

### Change 3 — Link auto-generation

New module `tools/links.py` with:

```python
def generate_report_links(
    alert: Alert,
    onchain_evidence: dict | None,
    affected_components: list[str],
) -> list[ReportLink]: ...
```

Behaviour:

1. **Transaction hashes** — for each known tx hash available in `alert.metadata` (keys like `src_initiate_tx_hash`, `dst_initiate_tx_hash`, `src_redeem_tx_hash`, etc.) and any hash appearing inside `onchain_evidence["findings"]` regex-matched as `0x[a-f0-9]{64}` or Bitcoin 64-hex strings, emit a `ReportLink(kind="tx")` using the chain's explorer URL template.

2. **Affected components** — for each entry in `affected_components` matching pattern `<repo-or-component>/<path>:L<line>` or `<path>:L<line>`, emit a `ReportLink(kind="code")` using `settings.gitea_url` + the gitea_repos(chain) mapping to produce:
   ```
   {gitea_url}/{gitea_org}/{repo_name}/src/branch/{branch}/{path}#L{line}
   ```

3. **Order link** — always emit a `ReportLink(kind="order", label="Order on Garden", url="https://garden.finance/orders/<order_id>")`. (Or use API URL if the app URL isn't stable; safer is the API URL which we already know works.)

Dedupe by URL. Cap at 12 links.

Block explorer templates live in a small constant per chain:

```python
_EXPLORER_TX_TEMPLATES = {
    "bitcoin":  "https://mempool.space/tx/{hash}",
    "ethereum": "https://etherscan.io/tx/{hash}",
    "arbitrum": "https://arbiscan.io/tx/{hash}",
    "base":     "https://basescan.org/tx/{hash}",
    "solana":   "https://solscan.io/tx/{hash}",
    "tron":     "https://tronscan.org/#/transaction/{hash}",
    "starknet": "https://starkscan.co/tx/{hash}",
    "litecoin": "https://litecoinspace.org/tx/{hash}",
    "alpen":    "https://explorer.testnet.alpenlabs.io/tx/{hash}",
    # fallback
}
```

The alert carries `metadata.source_chain` and `metadata.destination_chain` — use those (not the internal `alert.chain` literal) to pick the explorer per-hash.

### Change 4 — Orchestrator plumbs links into the report

In `agents/orchestrator.py` `run()`, after building the `RCAReport`, call `generate_report_links(...)` and attach before returning. Graceful: if the helper raises, log and proceed with `links=[]`.

### Change 5 — Discord rendering

In `discord_bot.py` `_build_rca_embed`:

- **Timeline** field — first 5 entries, each as `\`{ts}\` — {event}`. Appears between Route/Duration row and Investigation Summary.
- **What to do now** field — `next_action` rendered prominently, above Remediation Actions. Distinct emoji-free heading `▶ What to do now` so the reader sees it first.
- **Ruled out** field — first 3 entries as bullets. Appears after Remediation Actions.
- **Affected Components** — if a matching `ReportLink(kind="code")` exists, render as `• [<component>](<url>)`. Otherwise keep existing plain-text fallback.
- **Links** field — at the bottom, grouped by kind. Show `label`s as clickable markdown. Skip the field if empty.

Embed field count stays ≤25 (Discord limit); all new fields gated on non-empty values.

### Change 6 — No changes to orchestrator's investigate path

The early-return branch of `investigate()` already skips the RCA entirely; nothing to render, nothing to change.

---

## File change summary

| File | Action | Responsibility |
|---|---|---|
| `models/report.py` | Modify | Add `TimelineEvent`, `ReportLink`; add 4 fields on `RCAReport` |
| `tools/links.py` | Create | Pure function `generate_report_links` + explorer templates |
| `agents/specialists/base.py` | Modify | Extend JSON schema in prompt; parse new fields; return them |
| `agents/orchestrator.py` | Modify | Call link generator after assembling report |
| `discord_bot.py` | Modify | Render new fields in `_build_rca_embed` |
| `tests/links/__init__.py` | Create | Package marker |
| `tests/links/test_generate_links.py` | Create | Unit tests for link generation (offline, no HTTP) |
| `tests/specialist/test_tool_dispatch.py` | Modify | Extend the JSON-parse test to verify new fields round-trip |

## Testing

- `generate_report_links(alert, onchain_evidence, affected_components)` produces correct URLs per chain for sample tx hashes.
- Unknown chain name → no link (no crash).
- Malformed component string → skipped, no crash.
- `RCAReport` model accepts the new fields and tolerates missing ones.
- Specialist JSON parser returns empty list / empty string for missing new fields.

## Rollout

Single PR on `feat/report-shape-upgrade`, squash-merge to main.

## Risks

- **LLM returns unexpected shape for timeline** — e.g. a string instead of list of dicts. Mitigation: parse defensively; coerce to empty list on shape mismatch.
- **Explorer URLs drift** — templates are hardcoded, but chain explorers rarely change URL shape. Acceptable.
- **Affected-component regex fragile** — parser accepts only `foo/bar.rs:L42` or `foo/bar:L42` patterns, silently skips anything else. Acceptable — worst case is no link, specialist still reports the component name as plain text.
