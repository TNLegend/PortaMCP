from __future__ import annotations

import subprocess
from typing import Any

from ..context import AppContext
from ..result import fail, ok


def _truncate_utf8(text: str, max_bytes: int) -> str:
    cap = max(0, max_bytes)
    raw = text.encode("utf-8")
    if len(raw) <= cap:
        return text
    return raw[:cap].decode("utf-8", errors="ignore")


def _git(app: AppContext, repo: str, args: list[str], timeout: int = 60) -> dict:
    repo2 = app.policy.assert_path(repo)
    cp = subprocess.run(["git", "-C", repo2, *args], capture_output=True, text=True, errors="replace", timeout=timeout, check=False)
    cap = app.settings.max_command_output_bytes
    return {"returncode": cp.returncode, "stdout": _truncate_utf8(cp.stdout, cap), "stderr": _truncate_utf8(cp.stderr, cap)}


def register(mcp: Any, app: AppContext) -> None:
    @mcp.tool()
    def git_status(repo: str = ".") -> dict:
        """Get porcelain git status and current branch. Read-only."""
        try:
            return ok("git_status", {"status": _git(app, repo, ["status", "--short", "--branch"])})
        except Exception as e:
            return fail("git_status", e)

    @mcp.tool()
    def git_diff(repo: str = ".", staged: bool = False, path: str = "") -> dict:
        """Get git diff, optionally staged and path-scoped. Read-only."""
        try:
            args = ["diff"] + (["--cached"] if staged else [])
            if path:
                args += ["--", path]
            return ok("git_diff", _git(app, repo, args))
        except Exception as e:
            return fail("git_diff", e)

    @mcp.tool()
    def git_log(repo: str = ".", limit: int = 20) -> dict:
        """Return compact git history. Read-only."""
        try:
            return ok("git_log", _git(app, repo, ["log", f"-{max(1,min(limit,100))}", "--oneline", "--decorate"]))
        except Exception as e:
            return fail("git_log", e)

    @mcp.tool()
    def git_add(repo: str, paths: list[str]) -> dict:
        """Stage paths in a repository. Write action."""
        try:
            app.policy.assert_shell("git add")
            return ok("git_add", _git(app, repo, ["add", "--", *paths]))
        except Exception as e:
            return fail("git_add", e)

    @mcp.tool()
    def git_commit(repo: str, message: str) -> dict:
        """Create a local git commit. Write action. Does not push."""
        try:
            app.policy.assert_shell("git commit")
            return ok("git_commit", _git(app, repo, ["commit", "-m", message], timeout=120))
        except Exception as e:
            return fail("git_commit", e)
