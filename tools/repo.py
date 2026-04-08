"""
File-system tools for reading repos. Used by chain specialist agents.
All paths are sandboxed to the chain's component repo root from config.

Each chain has multiple component repos (executor, watcher, relayer, htlc).
Tools accept an optional `repo` parameter to select which component repo to use.
"""
import os
import subprocess
from config import settings

# Files/dirs to skip during study and grep operations
SKIP_PATTERNS = {
    "vendor", "node_modules", ".git", "__pycache__",
    "dist", "build", ".next", "target",
}
SKIP_EXTENSIONS = {
    ".pb.go", "_test.go", ".sum", ".lock", ".min.js",
    ".map", ".pyc", ".class",
}


def _resolve_repo_root(chain: str, repo: str) -> str:
    """
    Resolve the filesystem path for a chain + component repo name.
    Raises KeyError if the component name is unknown for this chain.
    """
    paths = settings.repo_paths(chain)
    if repo not in paths:
        available = ", ".join(paths.keys())
        raise KeyError(f"Unknown repo '{repo}' for chain '{chain}'. Available: {available}")
    return paths[repo]


def _safe_path(repo_root: str, relative_path: str) -> str:
    """Resolve and validate that path stays within repo root."""
    full = os.path.realpath(os.path.join(repo_root, relative_path))
    root = os.path.realpath(repo_root)
    if not full.startswith(root):
        raise ValueError(f"Path traversal attempt: {relative_path}")
    return full


def read_file(chain: str, path: str, repo: str = "executor") -> str:
    """
    Read a file from a specific component repo for the chain.

    Args:
        chain: Chain name (bitcoin, evm, solana)
        path: Relative path from repo root
        repo: Component repo name (executor, watcher, relayer, htlc, etc.)

    Returns:
        File contents as string (truncated to 8000 chars if very large)
    """
    try:
        repo_root = _resolve_repo_root(chain, repo)
    except KeyError as e:
        return f"[{e}]"
    try:
        full_path = _safe_path(repo_root, path)
        with open(full_path, "r", encoding="utf-8", errors="replace") as f:
            content = f.read()
        if len(content) > 8000:
            content = content[:8000] + f"\n\n[... truncated, file is {len(content)} chars total ...]"
        return content
    except FileNotFoundError:
        return f"[File not found: {repo}/{path}]"
    except Exception as e:
        return f"[Error reading {repo}/{path}: {e}]"


def grep_repo(chain: str, pattern: str, directory: str = ".", context_lines: int = 3, repo: str = "executor") -> str:
    """
    Search a component repo for a pattern using ripgrep (falls back to grep).

    Args:
        chain: Chain name
        pattern: Regex or literal pattern to search for
        directory: Subdirectory to search within (relative to repo root)
        context_lines: Lines of context to show around each match
        repo: Component repo name (executor, watcher, relayer, htlc, etc.)

    Returns:
        Matching lines with context, formatted as string
    """
    try:
        repo_root = _resolve_repo_root(chain, repo)
    except KeyError as e:
        return f"[{e}]"
    try:
        search_dir = _safe_path(repo_root, directory)
    except ValueError as e:
        return str(e)

    # Build exclude args
    exclude_dirs = " ".join(f"--exclude-dir={d}" for d in SKIP_PATTERNS)

    # Try ripgrep first, fall back to grep
    for cmd in [
        ["rg", "--no-heading", "-n", f"-C{context_lines}", pattern, search_dir],
        ["grep", "-r", "-n", f"--context={context_lines}", exclude_dirs, pattern, search_dir],
    ]:
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=15
            )
            output = result.stdout
            if len(output) > 6000:
                output = output[:6000] + "\n[... truncated ...]"
            return output if output else "[No matches found]"
        except FileNotFoundError:
            continue
        except subprocess.TimeoutExpired:
            return "[grep timed out]"

    return "[grep/rg not available]"


def list_directory(chain: str, path: str = ".", max_depth: int = 3, repo: str = "executor") -> str:
    """
    List directory tree of a component repo (skipping vendor/generated dirs).

    Args:
        chain: Chain name
        path: Relative path from repo root to list
        max_depth: Max depth to recurse (default 3)
        repo: Component repo name (executor, watcher, relayer, htlc, etc.)

    Returns:
        Tree-style directory listing as string
    """
    try:
        repo_root = _resolve_repo_root(chain, repo)
    except KeyError as e:
        return f"[{e}]"
    try:
        start = _safe_path(repo_root, path)
    except ValueError as e:
        return str(e)

    lines = []
    for root, dirs, files in os.walk(start):
        # Calculate depth relative to start
        depth = root.replace(start, "").count(os.sep)
        if depth >= max_depth:
            dirs.clear()
            continue

        # Prune skipped dirs in-place
        dirs[:] = [
            d for d in sorted(dirs)
            if d not in SKIP_PATTERNS and not d.startswith(".")
        ]

        indent = "  " * depth
        folder = os.path.basename(root)
        lines.append(f"{indent}{folder}/")

        sub_indent = "  " * (depth + 1)
        for f in sorted(files):
            # Skip generated/binary files
            if any(f.endswith(ext) for ext in SKIP_EXTENSIONS):
                continue
            lines.append(f"{sub_indent}{f}")

    result = "\n".join(lines)
    if len(result) > 6000:
        result = result[:6000] + "\n[... truncated ...]"
    return result if result else "[Empty directory]"


def build_repo_tool_definitions(chain: str) -> list[dict]:
    """
    Build repo tool definitions for a specific chain, including the available
    repo component names in the tool descriptions so the agent knows what to pass.
    """
    try:
        available_repos = list(settings.repo_paths(chain).keys())
        repo_desc = f"Component repo to use. Available for {chain}: {available_repos}. Default: 'executor'."
    except KeyError:
        repo_desc = "Component repo name (e.g. executor, watcher, relayer, htlc). Default: 'executor'."

    return [
        {
            "name": "read_file",
            "description": (
                "Read a source file from a component repo of this chain. "
                "Use this to inspect specific files the specialist needs to understand. "
                "Path is relative to the component repo root. "
                f"Specify 'repo' to target a specific component ({', '.join(available_repos)})."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path from repo root, e.g. 'executor/init.go'",
                    },
                    "repo": {
                        "type": "string",
                        "description": repo_desc,
                        "default": "executor",
                    },
                },
                "required": ["path"],
            },
        },
        {
            "name": "grep_repo",
            "description": (
                "Search a component repo for a pattern. Returns matching lines with context. "
                "Useful for finding error handling, function definitions, or specific log messages. "
                f"Specify 'repo' to target a specific component ({', '.join(available_repos)})."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Regex or literal string pattern to search for",
                    },
                    "directory": {
                        "type": "string",
                        "description": "Subdirectory to search in (default: '.' = whole repo)",
                        "default": ".",
                    },
                    "context_lines": {
                        "type": "integer",
                        "description": "Lines of context around each match (default 3)",
                        "default": 3,
                    },
                    "repo": {
                        "type": "string",
                        "description": repo_desc,
                        "default": "executor",
                    },
                },
                "required": ["pattern"],
            },
        },
        {
            "name": "list_directory",
            "description": (
                "List the directory tree of a component repo or a subdirectory. "
                "Use this first to understand the repo structure before reading files. "
                f"Specify 'repo' to target a specific component ({', '.join(available_repos)})."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path to list (default: '.' = repo root)",
                        "default": ".",
                    },
                    "max_depth": {
                        "type": "integer",
                        "description": "Max directory depth to show (default 3)",
                        "default": 3,
                    },
                    "repo": {
                        "type": "string",
                        "description": repo_desc,
                        "default": "executor",
                    },
                },
            },
        },
    ]




def execute_repo_tool(chain: str, tool_name: str, tool_input: dict) -> str:
    """Execute a repo tool call for the given chain."""
    repo = tool_input.get("repo", "executor")
    if tool_name == "read_file":
        return read_file(chain, tool_input["path"], repo)
    elif tool_name == "grep_repo":
        return grep_repo(
            chain,
            tool_input["pattern"],
            tool_input.get("directory", "."),
            tool_input.get("context_lines", 3),
            repo,
        )
    elif tool_name == "list_directory":
        return list_directory(
            chain,
            tool_input.get("path", "."),
            tool_input.get("max_depth", 3),
            repo,
        )
    return f"[Unknown repo tool: {tool_name}]"
