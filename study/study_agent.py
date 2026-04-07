"""
Study Mode Agent.
Reads a cloned chain repo and generates a rich knowledge doc at knowledge/{chain}.md.
Triggered via POST /study/{chain} — meant to be re-run whenever code changes significantly.
"""
import subprocess
import anthropic
from pathlib import Path

from config import settings
from tools.repo import execute_repo_tool


MODEL = "claude-opus-4-6"
KNOWLEDGE_DIR = Path(__file__).parent.parent / "knowledge"
INCIDENTS_DIR = Path(__file__).parent.parent / "incidents"

client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

SYSTEM_PROMPT = """\
You are a code study agent. Your job is to deeply read a blockchain executor/watcher/relayer
codebase and produce a rich knowledge document for an AI-powered RCA (Root Cause Analysis) system.

This knowledge doc will be embedded into the system prompt of a chain specialist AI agent
that must diagnose production incidents — so completeness and precision are critical.

The knowledge doc you generate must cover:

## 1. Service Architecture Overview
- What services exist (executor, watcher, relayer)?
- How do they interact?
- What is the order lifecycle on this chain?

## 2. Key Files and Their Roles
- Entry points, main loops, and their responsibilities
- Core business logic files
- Configuration and constants files
- Error definitions

## 3. Critical Functions
- Functions that handle order initiation, redemption, refund
- Fee estimation and submission logic
- State machine transitions
- Retry and timeout handling

## 4. Known Failure Patterns
- What are the most common ways things go wrong?
- What error messages appear in logs when each failure occurs?
- What on-chain conditions trigger each failure?

## 5. Important Constants and Thresholds
- Timeouts (order deadlines, retry intervals, confirmation waits)
- Fee multipliers or minimum fee thresholds
- Max retry counts
- Any chain-specific magic numbers

## 6. Log Signatures
- Key log messages and what they mean operationally
- How to distinguish a transient error from a fatal one
- Log patterns that indicate specific failure modes

## 7. On-Chain Checks per Failure Type
- For each alert_type (missed_init, deadline_approaching, etc.):
  What should an on-chain agent check? What confirms or rules out each hypothesis?

Be thorough. Use the repo tools to read actual source code — don't guess.
Skip test files, vendor directories, and generated protobuf files.
Focus on: entry points, core logic, error handling, constants, config.
"""


def _checkout_and_pull(path: str, branch: str) -> None:
    """
    Checkout the target branch and pull latest (best-effort; failures are silently ignored).
    This ensures the study agent reads the code that's actually deployed.
    """
    try:
        subprocess.run(["git", "-C", path, "checkout", branch], check=True, capture_output=True)
        subprocess.run(["git", "-C", path, "pull", "--ff-only"], check=True, capture_output=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass


def run(chain: str) -> str:
    """
    Study all component repos for a chain and write knowledge/{chain}.md.

    For bitcoin: executor, watcher_cobi, watcher_zmq, relayer, htlc
    For evm:     executor, watcher, relayer, htlc
    For solana:  executor, watcher, relayer, htlc
    For spark:   executor

    Args:
        chain: Chain name — bitcoin, evm, solana, or spark

    Returns:
        Path to the written knowledge file as a string
    """
    repo_map = settings.repo_paths(chain)
    branch_map = settings.repo_branches(chain)

    # Validate all repos exist, checkout correct branch, pull latest
    missing = []
    for component, path in repo_map.items():
        if not Path(path).exists():
            missing.append(f"{component} → {path}")
        else:
            branch = branch_map.get(component, "staging")
            _checkout_and_pull(path, branch)

    if missing:
        raise FileNotFoundError(
            f"Missing repos for chain '{chain}'. Clone these first:\n" + "\n".join(missing)
        )

    # Load past incidents if available
    incidents_context = ""
    incidents_path = INCIDENTS_DIR / f"{chain}.yaml"
    if incidents_path.exists():
        incidents_context = (
            f"\n\nPast incidents for this chain (pay special attention to files mentioned here):\n"
            f"```yaml\n{incidents_path.read_text()}\n```\n"
        )

    # Describe each repo to the agent (include branch so it's in the knowledge doc)
    repo_listing = "\n".join(
        f"  - repo='{component}' → {path} (branch: {branch_map.get(component, 'staging')})"
        for component, path in repo_map.items()
    )

    user_message = (
        f"Study all {chain} service repos. There are {len(repo_map)} component repos:\n"
        f"{repo_listing}\n\n"
        f"Use the 'repo' parameter in your tools to switch between components. "
        f"Start by listing the root directory of each repo to understand the project structure. "
        f"Then systematically read the most important files in each — entry points, core logic, "
        f"error handling, fee estimation, retry logic, and configuration. "
        f"Skip: vendor/, node_modules/, *_test.go, *.pb.go, *.generated.*, dist/, build/\n"
        f"{incidents_context}\n"
        f"After reading enough to have a comprehensive understanding of ALL components, "
        f"write a single detailed knowledge document following the 7-section structure from "
        f"your instructions. Cover each component (executor, watcher(s), relayer, htlc) in context "
        f"— how they interact, where they hand off to each other, and how failures propagate. "
        f"Be specific — include function names, file paths (with repo prefix), and actual constant values."
    )

    # Build chain-aware tool definitions so the agent sees available repo names
    from tools.repo import build_repo_tool_definitions
    tool_defs = build_repo_tool_definitions(chain)

    messages = [{"role": "user", "content": user_message}]

    # Agentic loop with repo tools
    while True:
        response = client.messages.create(
            model=MODEL,
            max_tokens=16000,
            thinking={"type": "adaptive"},
            output_config={"effort": "high"},
            system=SYSTEM_PROMPT,
            tools=tool_defs,
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
            result = execute_repo_tool(chain, tool.name, tool.input)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tool.id,
                "content": result,
            })

        messages.append({"role": "user", "content": tool_results})

    knowledge_text = next(
        (b.text for b in response.content if b.type == "text"),
        "[Study agent returned no output]",
    )

    # Write to disk
    KNOWLEDGE_DIR.mkdir(exist_ok=True)
    output_path = KNOWLEDGE_DIR / f"{chain}.md"
    output_path.write_text(knowledge_text, encoding="utf-8")

    return str(output_path)
