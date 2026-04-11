"""
Gitea API tools for reading source code from remote repos.
Used by chain specialist agents when repos are not mounted on disk (prod).
Falls back gracefully if Gitea is not configured.

Supports: read_file, search_code, list_directory — same interface as repo tools.
"""
import base64
import logging
import httpx
from functools import lru_cache

from config import settings

logger = logging.getLogger("rca-agent.gitea")

_TIMEOUT = 15.0  # seconds per API call

# Files/dirs to skip in directory listings (mirrors repo.py)
SKIP_PATTERNS = {
    "vendor", "node_modules", ".git", "__pycache__",
    "dist", "build", ".next", "target",
}
SKIP_EXTENSIONS = {
    ".pb.go", "_test.go", ".sum", ".lock", ".min.js",
    ".map", ".pyc", ".class",
}


def _headers() -> dict:
    return {"Authorization": f"token {settings.gitea_token}"}


def _api(path: str) -> str:
    return f"{settings.gitea_url.rstrip('/')}/api/v1{path}"


def _resolve_gitea_repo(chain: str, component: str) -> tuple[str, str]:
    """
    Resolve a chain + component name to (gitea_repo_name, branch).
    Raises KeyError if unknown.
    """
    repos = settings.gitea_repos(chain)
    if component not in repos:
        available = ", ".join(repos.keys())
        raise KeyError(
            f"Unknown component '{component}' for chain '{chain}'. "
            f"Available: {available}"
        )
    return repos[component]


def is_configured() -> bool:
    """Check if Gitea access is configured."""
    return bool(settings.gitea_url and settings.gitea_token)


def read_file(chain: str, path: str, repo: str = "executor") -> str:
    """
    Read a file from a Gitea repo via API.

    Args:
        chain: Chain name (bitcoin, evm, solana)
        path: Relative path from repo root
        repo: Component name (executor, watcher, relayer, etc.)

    Returns:
        File contents as string (truncated to 8000 chars if large)
    """
    try:
        repo_name, branch = _resolve_gitea_repo(chain, repo)
    except KeyError as e:
        return f"[{e}]"

    owner = settings.gitea_org
    # URL-encode path segments
    encoded_path = path.lstrip("/")
    url = _api(f"/repos/{owner}/{repo_name}/contents/{encoded_path}")

    try:
        resp = httpx.get(
            url,
            headers=_headers(),
            params={"ref": branch},
            timeout=_TIMEOUT,
        )
        if resp.status_code == 404:
            return f"[File not found: {repo}/{path} (branch: {branch})]"
        resp.raise_for_status()
        data = resp.json()

        # Gitea returns base64-encoded content for files
        if isinstance(data, list):
            return f"[Path is a directory, not a file: {repo}/{path}. Use list_directory instead.]"

        content_b64 = data.get("content", "")
        if not content_b64:
            return f"[Empty file: {repo}/{path}]"

        content = base64.b64decode(content_b64).decode("utf-8", errors="replace")
        if len(content) > 8000:
            content = content[:8000] + f"\n\n[... truncated, file is {len(content)} chars total ...]"
        return content

    except httpx.HTTPStatusError as e:
        return f"[Gitea API error reading {repo}/{path}: HTTP {e.response.status_code}]"
    except Exception as e:
        return f"[Gitea API error: {e}]"


def search_code(chain: str, pattern: str, repo: str = "executor") -> str:
    """
    Search a Gitea repo for a pattern using the code search API.
    Falls back to reading the file tree and searching key files if code search
    is not available.

    Args:
        chain: Chain name
        pattern: Text pattern to search for
        repo: Component name

    Returns:
        Matching lines with context
    """
    try:
        repo_name, branch = _resolve_gitea_repo(chain, repo)
    except KeyError as e:
        return f"[{e}]"

    owner = settings.gitea_org

    # Try Gitea's topic/code search first
    url = _api(f"/repos/{owner}/{repo_name}/topics")

    # Gitea's code search: GET /repos/{owner}/{repo}/contents with search
    # Unfortunately Gitea doesn't have a native grep/code-search API.
    # We'll use git search via the API if available, otherwise use tree + read approach.

    # Strategy: Get the file tree, find likely files, read and grep them locally
    try:
        tree_url = _api(f"/repos/{owner}/{repo_name}/git/trees/{branch}")
        resp = httpx.get(
            tree_url,
            headers=_headers(),
            params={"recursive": "true"},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        tree = resp.json()

        # Filter to source files only
        source_extensions = {
            ".go", ".rs", ".ts", ".js", ".py", ".sol", ".toml", ".yaml", ".yml",
            ".json", ".tsx", ".jsx",
        }
        candidate_files = []
        for entry in tree.get("tree", []):
            if entry.get("type") != "blob":
                continue
            path = entry.get("path", "")
            # Skip vendored / generated
            parts = path.split("/")
            if any(p in SKIP_PATTERNS for p in parts):
                continue
            if any(path.endswith(ext) for ext in SKIP_EXTENSIONS):
                continue
            # Only source files
            if any(path.endswith(ext) for ext in source_extensions):
                candidate_files.append(path)

        # Search through files — limit to avoid excessive API calls
        # Prioritize files with the pattern in their name first
        pattern_lower = pattern.lower()
        name_matches = [f for f in candidate_files if pattern_lower in f.lower()]
        other_files = [f for f in candidate_files if f not in name_matches]

        # Read up to 20 files (name matches first, then others)
        files_to_check = (name_matches + other_files)[:20]

        results = []
        for file_path in files_to_check:
            content = read_file(chain, file_path, repo)
            if content.startswith("["):
                continue  # skip errors
            lines = content.split("\n")
            for i, line in enumerate(lines):
                if pattern_lower in line.lower():
                    # Show context: 2 lines before, the match, 2 lines after
                    start = max(0, i - 2)
                    end = min(len(lines), i + 3)
                    context = "\n".join(
                        f"{'>' if j == i else ' '} {j+1}: {lines[j]}"
                        for j in range(start, end)
                    )
                    results.append(f"--- {repo_name}/{file_path} ---\n{context}")

            if len(results) >= 15:
                break

        if not results:
            return f"[No matches for '{pattern}' in {repo_name} (searched {len(files_to_check)} files)]"

        output = "\n\n".join(results)
        if len(output) > 6000:
            output = output[:6000] + "\n[... truncated ...]"
        return output

    except Exception as e:
        return f"[Gitea search error: {e}]"


def list_directory(chain: str, path: str = ".", max_depth: int = 3, repo: str = "executor") -> str:
    """
    List directory tree of a Gitea repo.

    Args:
        chain: Chain name
        path: Relative path (default: root)
        max_depth: Max depth to show
        repo: Component name

    Returns:
        Tree-style directory listing
    """
    try:
        repo_name, branch = _resolve_gitea_repo(chain, repo)
    except KeyError as e:
        return f"[{e}]"

    owner = settings.gitea_org

    if path == ".":
        # Use git tree API for full recursive listing (much faster)
        try:
            tree_url = _api(f"/repos/{owner}/{repo_name}/git/trees/{branch}")
            resp = httpx.get(
                tree_url,
                headers=_headers(),
                params={"recursive": "true"},
                timeout=_TIMEOUT,
            )
            resp.raise_for_status()
            tree = resp.json()

            lines = [f"{repo_name}/"]
            for entry in sorted(tree.get("tree", []), key=lambda e: e.get("path", "")):
                entry_path = entry.get("path", "")
                parts = entry_path.split("/")

                # Skip deep entries
                if len(parts) > max_depth:
                    continue
                # Skip vendored dirs
                if any(p in SKIP_PATTERNS for p in parts):
                    continue
                # Skip generated files
                if any(entry_path.endswith(ext) for ext in SKIP_EXTENSIONS):
                    continue

                indent = "  " * len(parts)
                name = parts[-1]
                if entry.get("type") == "tree":
                    lines.append(f"{indent}{name}/")
                else:
                    lines.append(f"{indent}{name}")

            result = "\n".join(lines)
            if len(result) > 6000:
                result = result[:6000] + "\n[... truncated ...]"
            return result

        except Exception as e:
            return f"[Gitea tree error: {e}]"
    else:
        # Use contents API for specific subdirectory
        encoded_path = path.lstrip("/")
        url = _api(f"/repos/{owner}/{repo_name}/contents/{encoded_path}")
        try:
            resp = httpx.get(
                url,
                headers=_headers(),
                params={"ref": branch},
                timeout=_TIMEOUT,
            )
            if resp.status_code == 404:
                return f"[Directory not found: {repo}/{path}]"
            resp.raise_for_status()
            data = resp.json()

            if not isinstance(data, list):
                return f"[Path is a file, not a directory: {repo}/{path}]"

            lines = [f"{path}/"]
            for entry in sorted(data, key=lambda e: (e.get("type", "") != "dir", e.get("name", ""))):
                name = entry.get("name", "")
                if name in SKIP_PATTERNS or name.startswith("."):
                    continue
                if entry.get("type") == "dir":
                    lines.append(f"  {name}/")
                else:
                    if any(name.endswith(ext) for ext in SKIP_EXTENSIONS):
                        continue
                    lines.append(f"  {name}")

            return "\n".join(lines)

        except Exception as e:
            return f"[Gitea directory error: {e}]"


def execute_gitea_tool(chain: str, tool_name: str, tool_input: dict) -> str:
    """Execute a Gitea tool call for the given chain."""
    repo = tool_input.get("repo", "executor")
    if tool_name == "read_file":
        return read_file(chain, tool_input["path"], repo)
    elif tool_name == "grep_repo":
        return search_code(chain, tool_input["pattern"], repo)
    elif tool_name == "list_directory":
        return list_directory(
            chain,
            tool_input.get("path", "."),
            tool_input.get("max_depth", 3),
            repo,
        )
    return f"[Unknown gitea tool: {tool_name}]"


def build_gitea_tool_definitions(chain: str) -> list[dict]:
    """
    Build tool definitions for Gitea-based code access.
    Same interface as repo tools so the specialist doesn't need different prompts.
    """
    repos = settings.gitea_repos(chain)
    available_repos = list(repos.keys())
    repo_desc = (
        f"Component repo to use. Available for {chain}: {available_repos}. "
        f"Default: 'executor'."
    )

    return [
        {
            "name": "read_file",
            "description": (
                "Read a source file from a component repo via Gitea API. "
                "Use this to inspect specific files. "
                "Path is relative to the repo root. "
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
                "Search a component repo for a text pattern. Returns matching lines with context. "
                "Searches source files (.go, .rs, .ts, .js, .sol, .py, .toml). "
                f"Specify 'repo' to target a specific component ({', '.join(available_repos)})."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Text pattern to search for (case-insensitive)",
                    },
                    "directory": {
                        "type": "string",
                        "description": "Not used for Gitea search (searches whole repo)",
                        "default": ".",
                    },
                    "context_lines": {
                        "type": "integer",
                        "description": "Not used for Gitea search",
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
                "List the directory tree of a component repo via Gitea API. "
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
