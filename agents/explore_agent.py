"""
Explore Agent — answers natural language questions about the codebase.

Uses Gitea tools to navigate repos, read files, and search code.
Resolves which repo to focus on from the question text, falling back to
keyword matching against the known repo registry.
"""
import re
import logging
from pathlib import Path

from config import settings
from providers import get_provider
from providers.base import TokenUsage
from models.pricing import compute_cost
from tools.gitea import (
    is_configured as gitea_configured,
    build_explore_tool_definitions,
    execute_explore_tool,
    list_org_repos,
)

logger = logging.getLogger("rca-agent.explore")

KNOWLEDGE_DIR = Path(__file__).parent.parent / "knowledge"


# ── Repo registry ──────────────────────────────────────────────────────────
# Flat map of every known repo name → default branch.
# Built from all chain repos + solver repos + shared libs.

def _build_repo_registry() -> dict[str, str]:
    """
    Build a flat {gitea_repo_name: branch} map from all configured repos.
    This includes all chains (bitcoin, evm, solana), solver repos, and shared libs.
    """
    registry: dict[str, str] = {}
    for chain in ("bitcoin", "evm", "solana"):
        for _component, (repo_name, branch) in settings.gitea_repos(chain).items():
            registry[repo_name] = branch
    for _component, (repo_name, branch) in settings.gitea_solver_repos().items():
        registry[repo_name] = branch
    return registry


# Aliases: common names / abbreviations → actual Gitea repo name
_ALIASES: dict[str, str] = {
    "cobi": "cobi-v2",
    "cobi-v2": "cobi-v2",
    "bitcoin-executor": "cobi-v2",
    "bit-ponder": "bit-ponder",
    "bitcoin-watcher": "bitcoin-watcher",
    "btc-relayer": "btc-relayer",
    "evm-executor": "evm-executor",
    "evm-watcher": "garden-evm-watcher",
    "garden-evm-watcher": "garden-evm-watcher",
    "evm-relayer": "evm-swapper-relay",
    "evm-swapper-relay": "evm-swapper-relay",
    "evm-htlc": "evm-htlc",
    "solana-executor": "solana-executor",
    "solana-watcher": "solana-watcher",
    "solana-relayer": "solana-relayer",
    "solana-native-swaps": "solana-native-swaps",
    "solana-spl-swaps": "solana-spl-swaps",
    "blockchain": "blockchain",
    "garden-rs": "garden-rs",
    "solver-engine": "solver-engine",
    "solver-comms": "solver-comms",
    "solver-agg": "solver-agg-v2",
    "solver-agg-v2": "solver-agg-v2",
    "solver": "solver",
    "solver-daemon": "solver",
}

# Keyword hints: if these appear in the question, bias toward a repo.
# Maps keyword → gitea repo name.
_KEYWORD_HINTS: dict[str, str] = {
    "price protection": "cobi-v2",
    "price_protection": "cobi-v2",
    "htlc": "evm-htlc",
    "atomic swap": "evm-htlc",
    "tapscript": "cobi-v2",
    "redeem": "cobi-v2",
    "refund": "cobi-v2",
    "initiate": "cobi-v2",
    "bitcoin executor": "cobi-v2",
    "evm executor": "evm-executor",
    "solana executor": "solana-executor",
    "watcher cobi": "bit-ponder",
    "bit-ponder": "bit-ponder",
    "zmq": "bitcoin-watcher",
    "mempool": "bitcoin-watcher",
    "relay": "btc-relayer",
    "btc relay": "btc-relayer",
    "evm relay": "evm-swapper-relay",
    "solana relay": "solana-relayer",
    "solver engine": "solver-engine",
    "solver comms": "solver-comms",
    "solver agg": "solver-agg-v2",
    "aggregator": "solver-agg-v2",
    "solver daemon": "solver",
    "native swap": "solana-native-swaps",
    "spl swap": "solana-spl-swaps",
    "spl token": "solana-spl-swaps",
    "blockchain lib": "blockchain",
    "garden-rs": "garden-rs",
}


def resolve_repo(question: str) -> tuple[str, str] | None:
    """
    Try to resolve a Gitea repo name and branch from the question text.

    Strategy:
    1. Direct regex match: look for known repo names in the question.
    2. Alias match: check if any alias appears in the question.
    3. Keyword hints: check domain-specific keywords.
    4. Return None if no match — agent will search across repos.

    Returns (repo_name, branch) or None.
    """
    registry = _build_repo_registry()
    q_lower = question.lower()

    # 1. Direct repo name match (longest match first to avoid partial hits)
    for repo_name in sorted(registry.keys(), key=len, reverse=True):
        if re.search(r'\b' + re.escape(repo_name) + r'\b', q_lower):
            return (repo_name, registry[repo_name])

    # 2. Alias match (longest alias first)
    for alias in sorted(_ALIASES.keys(), key=len, reverse=True):
        if re.search(r'\b' + re.escape(alias) + r'\b', q_lower):
            repo_name = _ALIASES[alias]
            branch = registry.get(repo_name, "main")
            return (repo_name, branch)

    # 3. Keyword hints
    for keyword in sorted(_KEYWORD_HINTS.keys(), key=len, reverse=True):
        if keyword in q_lower:
            repo_name = _KEYWORD_HINTS[keyword]
            branch = registry.get(repo_name, "main")
            return (repo_name, branch)

    return None


def _load_relevant_knowledge(repo_name: str) -> str:
    """Load knowledge docs relevant to the target repo, if any."""
    # Map repo names to knowledge doc chains
    repo_to_chain: dict[str, str] = {}
    for chain in ("bitcoin", "evm", "solana"):
        for _component, (rname, _branch) in settings.gitea_repos(chain).items():
            repo_to_chain[rname] = chain

    chain = repo_to_chain.get(repo_name)
    if not chain:
        # Solver repos get solver knowledge
        solver_repos = {rname for rname, _b in settings.gitea_solver_repos().values()}
        if repo_name in solver_repos:
            path = KNOWLEDGE_DIR / "solver.md"
            if path.exists():
                return path.read_text(encoding="utf-8")
        return ""

    # Load chain-specific + solver knowledge
    parts = []
    chain_path = KNOWLEDGE_DIR / f"{chain}.md"
    if chain_path.exists():
        parts.append(chain_path.read_text(encoding="utf-8"))
    solver_path = KNOWLEDGE_DIR / "solver.md"
    if solver_path.exists():
        parts.append(solver_path.read_text(encoding="utf-8"))
    return "\n\n---\n\n".join(parts)


def _build_system_prompt(repo_name: str, branch: str, knowledge: str) -> list[dict]:
    """Build the system prompt blocks for the explore agent."""
    base_prompt = (
        "You are a codebase exploration agent for the Garden Finance cross-chain bridge ecosystem.\n\n"
        "Your job is to answer questions about the codebase by reading source code from Gitea repos.\n"
        "You have tools to read files, search for patterns, and list directories.\n\n"
        "## Guidelines\n\n"
        "- Be precise: cite file paths and line numbers when referencing code.\n"
        "- Be thorough: if the first search doesn't find what you need, try alternative patterns.\n"
        "- Be concise: answer the question directly, then provide supporting evidence.\n"
        "- When showing code, quote the relevant snippet — don't just say 'see file X'.\n"
        "- If you cannot find the answer, say so clearly rather than guessing.\n\n"
        f"## Target Repository\n\n"
        f"You are primarily exploring **{repo_name}** (branch: `{branch}`).\n"
        "Start by listing the repo structure if needed, then drill into relevant files.\n"
    )

    blocks = [{"type": "text", "text": base_prompt}]

    if knowledge:
        blocks.append({
            "type": "text",
            "text": f"\n\n## Knowledge Base\n\nUse this pre-generated knowledge to guide your search. "
                    f"It describes the architecture and key code paths:\n\n{knowledge}",
            "cache_control": {"type": "ephemeral"},
        })

    return blocks


def run(question: str) -> dict:
    """
    Answer a natural language question about the codebase.

    Returns:
        dict with 'answer', 'repo_name', 'branch', 'usage' (token counts + cost)
    """
    if not gitea_configured():
        return {
            "answer": "Gitea is not configured. Cannot explore repos.",
            "repo_name": None,
            "branch": None,
            "usage": {},
        }

    # Resolve target repo
    resolved = resolve_repo(question)
    if resolved:
        repo_name, branch = resolved
        logger.info("Resolved repo from question: %s (branch: %s)", repo_name, branch)
    else:
        # Fallback: let the agent figure it out — default to listing org repos
        # We'll give the agent a broader set of tools and tell it to pick the right repo
        repo_name, branch = None, None
        logger.info("Could not resolve repo from question, agent will search across repos")

    # Build tools and system prompt
    if repo_name:
        knowledge = _load_relevant_knowledge(repo_name)
        tool_defs = build_explore_tool_definitions(repo_name, branch)
        system = _build_system_prompt(repo_name, branch, knowledge)
    else:
        # No repo resolved — give the agent a generic prompt and let it start with org repo list
        registry = _build_repo_registry()
        repo_list = "\n".join(f"- **{name}** (branch: `{br}`)" for name, br in sorted(registry.items()))
        system = [{
            "type": "text",
            "text": (
                "You are a codebase exploration agent for the Garden Finance cross-chain bridge ecosystem.\n\n"
                "The user asked a question but did not specify which repo. "
                "Here are all available repos:\n\n"
                f"{repo_list}\n\n"
                "Based on the question, pick the most likely repo, then use your tools to explore it.\n"
                "Be precise: cite file paths and line numbers.\n"
            ),
        }]
        # Use the first repo as a placeholder — the agent will override via tool input
        first_repo = next(iter(_build_repo_registry()))
        first_branch = _build_repo_registry()[first_repo]
        tool_defs = build_explore_tool_definitions(first_repo, first_branch)

    provider = get_provider()
    model = settings.get_specialist_model()
    messages = [{"role": "user", "content": question}]

    total_input = total_output = total_cache_read = total_cache_write = 0

    def _accumulate(resp) -> None:
        nonlocal total_input, total_output, total_cache_read, total_cache_write
        u = resp.usage
        total_input += u.input_tokens
        total_output += u.output_tokens
        total_cache_read += u.cache_read_tokens
        total_cache_write += u.cache_creation_tokens

    max_turns = 15

    response = None
    for _turn in range(max_turns):
        response = provider.create_message(
            model=model,
            max_tokens=8192,
            system=system,
            tools=tool_defs,
            messages=messages,
        )
        _accumulate(response)

        if response.stop_reason == "end_turn":
            break

        if not response.tool_calls:
            break

        messages.append(provider.build_assistant_message(response))

        tool_results = []
        for tc in response.tool_calls:
            result = execute_explore_tool(tc.name, tc.input)
            tool_results.append((tc.id, result))

        tr_msg = provider.build_tool_results_message(tool_results)
        if isinstance(tr_msg, list):
            messages.extend(tr_msg)
        else:
            messages.append(tr_msg)

    # Handle turn-cap exhaustion (same pattern as specialist)
    if response and not response.text and response.tool_calls:
        messages.append(provider.build_assistant_message(response))
        stub_results = [
            (tc.id, "Tool call limit reached — no result available.")
            for tc in response.tool_calls
        ]
        tr_msg = provider.build_tool_results_message(stub_results)
        if isinstance(tr_msg, list):
            messages.extend(tr_msg)
        else:
            messages.append(tr_msg)

        messages.append({
            "role": "user",
            "content": (
                "You have used the maximum number of tool calls. "
                "Based on everything gathered so far, answer the question now."
            ),
        })
        response = provider.create_message(
            model=model,
            max_tokens=8192,
            system=system,
            messages=messages,
        )
        _accumulate(response)

    answer = (response.text if response else "") or "[Explore agent returned no answer]"
    cost = compute_cost(model, total_input, total_output, total_cache_read, total_cache_write)

    return {
        "answer": answer,
        "repo_name": repo_name,
        "branch": branch,
        "usage": {
            "model": model,
            "input_tokens": total_input,
            "output_tokens": total_output,
            "cache_read_tokens": total_cache_read,
            "cache_write_tokens": total_cache_write,
            "cost_usd": round(cost, 6),
        },
    }
