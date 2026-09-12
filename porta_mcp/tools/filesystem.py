from __future__ import annotations

import base64
import fnmatch
import hashlib
import os
import shutil
import stat
import time
from pathlib import Path
from typing import Any

from ..context import AppContext
from ..policy import PolicyError
from ..result import fail, ok

FILESYSTEM_TRAVERSAL_BUDGET_SECONDS = 30


def _ensure_writable(path: str | Path) -> None:
    """Clear a Windows read-only attribute while preserving other mode bits."""
    if os.name != "nt":
        return
    p = Path(path)
    if p.exists():
        os.chmod(p, os.stat(p).st_mode | stat.S_IWRITE)


def _rmtree_onerror(func: Any, path: str, exc_info: Any) -> None:
    """Retry Windows read-only entries during recursive removal."""
    exc = exc_info[1]
    if isinstance(exc, PermissionError) and os.name == "nt":
        _ensure_writable(path)
        func(path)
        return
    raise exc


def _unlink_readonly(path: Path) -> None:
    """Unlink a file, retrying once after clearing a Windows read-only attribute."""
    try:
        path.unlink()
    except PermissionError:
        if os.name != "nt":
            raise
        _ensure_writable(path)
        path.unlink()


def _copy2_readonly_retry(src: str, dst: str) -> str:
    """Copy a file, retrying when an explicit overwrite hits a read-only target."""
    try:
        return shutil.copy2(src, dst)
    except PermissionError:
        if os.name != "nt":
            raise
        _ensure_writable(dst)
        return shutil.copy2(src, dst)


def _is_symlink_or_junction(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def _assert_safe_copy_tree(root: Path) -> None:
    """Reject recursive copies that would traverse a symlink or Windows junction."""
    if _is_symlink_or_junction(root):
        raise ValueError("Directory copy source cannot be a symlink or junction")
    for current, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        base = Path(current)
        for name in [*dirnames, *filenames]:
            entry = base / name
            if _is_symlink_or_junction(entry):
                raise ValueError(f"Directory copy source contains a symlink or junction: {entry}")


def _assert_tree_policy(app: AppContext, root: Path, *, destructive: bool) -> None:
    _assert_safe_copy_tree(root)
    for current, dirs, files in os.walk(root, followlinks=False):
        for name in [*dirs, *files]:
            app.policy.assert_path(str(Path(current) / name), write=True, destructive=destructive)


def register(mcp: Any, app: AppContext) -> None:
    @mcp.tool()
    def fs_list(path: str, recursive: bool = False, pattern: str = "*", limit: int = 1000) -> dict:
        """List files/directories inside an allowed root. Read-only."""
        try:
            p = Path(app.policy.assert_path(path))
            limit = max(1, min(limit, 5000))
            it = p.rglob(pattern) if recursive else p.glob(pattern)
            rows = []
            scanned = 0
            timed_out = False
            started = time.monotonic()
            for x in it:
                scanned += 1
                if recursive and time.monotonic() - started >= FILESYSTEM_TRAVERSAL_BUDGET_SECONDS:
                    timed_out = True
                    break
                try:
                    app.policy.assert_path(str(x))
                    st = x.stat()
                    rows.append({"path": str(x), "name": x.name, "is_dir": x.is_dir(), "size": st.st_size, "mtime": st.st_mtime})
                except (OSError, PolicyError):
                    continue
                if len(rows) >= limit:
                    break
            truncated = timed_out or len(rows) >= limit
            warnings = []
            if timed_out:
                warnings.append(
                    f"recursive listing stopped after {FILESYSTEM_TRAVERSAL_BUDGET_SECONDS}s"
                )
            app.audit.write(
                "fs_list",
                target=str(p),
                detail={"recursive": recursive, "scanned": scanned, "returned": len(rows), "timed_out": timed_out},
            )
            return ok(
                "fs_list",
                {"items": rows, "truncated": truncated, "timed_out": timed_out, "scanned": scanned},
                warnings,
            )
        except Exception as e:
            return fail("fs_list", e)

    @mcp.tool()
    def fs_stat(path: str) -> dict:
        """Return metadata for one allowed path. Read-only."""
        try:
            p = Path(app.policy.assert_path(path))
            st = p.stat()
            return ok("fs_stat", {"path": str(p), "is_file": p.is_file(), "is_dir": p.is_dir(), "size": st.st_size, "mtime": st.st_mtime, "mode": st.st_mode})
        except Exception as e:
            return fail("fs_stat", e)

    @mcp.tool()
    def fs_read_text(path: str, start_line: int = 1, end_line: int = 0) -> dict:
        """Read a UTF-8/text file with optional 1-based line range. Read-only and size limited."""
        try:
            p = Path(app.policy.assert_path(path))
            size = p.stat().st_size
            if size > app.settings.max_read_bytes:
                raise ValueError(f"File exceeds max_read_bytes ({size} > {app.settings.max_read_bytes})")
            text = p.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines()
            a = max(1, start_line)
            b = len(lines) if end_line <= 0 else min(end_line, len(lines))
            selected = "\n".join(lines[a - 1:b])
            return ok("fs_read_text", {"path": str(p), "start_line": a, "end_line": b, "total_lines": len(lines), "text": selected})
        except Exception as e:
            return fail("fs_read_text", e)

    @mcp.tool()
    def fs_read_bytes(path: str, max_bytes: int = 262144) -> dict:
        """Read a small binary file as base64. Read-only. Prefer specialized tools for large files/images."""
        try:
            p = Path(app.policy.assert_path(path))
            n = min(max(1, max_bytes), app.settings.max_read_bytes)
            with p.open("rb") as handle:
                data = handle.read(n + 1)
            if len(data) > n:
                raise ValueError(f"File is {len(data)} bytes; requested limit is {n}")
            return ok("fs_read_bytes", {"path": str(p), "base64": base64.b64encode(data).decode("ascii"), "size": len(data)})
        except Exception as e:
            return fail("fs_read_bytes", e)

    @mcp.tool()
    def fs_write_text(path: str, text: str, create_parents: bool = False, overwrite: bool = True) -> dict:
        """Write UTF-8 text inside allowed roots. Write action; capped by max_write_bytes."""
        try:
            p = Path(app.policy.assert_path(path, write=True))
            raw = text.encode("utf-8")
            if len(raw) > app.settings.max_write_bytes:
                raise ValueError("Write exceeds max_write_bytes")
            if p.exists() and not overwrite:
                raise FileExistsError(str(p))
            if create_parents:
                p.parent.mkdir(parents=True, exist_ok=True)
            if p.exists() and overwrite:
                _ensure_writable(p)
            p.write_text(text, encoding="utf-8")
            stored_bytes = p.stat().st_size
            app.audit.write("fs_write_text", target=str(p), detail={"bytes": stored_bytes})
            return ok("fs_write_text", {"path": str(p), "bytes": stored_bytes})
        except Exception as e:
            return fail("fs_write_text", e)

    @mcp.tool()
    def fs_patch_text(path: str, old: str, new: str, expected_replacements: int = 1) -> dict:
        """Patch a text file by exact string replacement. Safer than rewriting the whole file."""
        try:
            p = Path(app.policy.assert_path(path, write=True))
            if p.stat().st_size > app.settings.max_read_bytes:
                raise ValueError("File exceeds max_read_bytes")
            text = p.read_text(encoding="utf-8")
            count = text.count(old)
            if count != expected_replacements:
                raise ValueError(f"Expected {expected_replacements} matches, found {count}; no changes made")
            updated = text.replace(old, new)
            if len(updated.encode("utf-8")) > app.settings.max_write_bytes:
                raise ValueError("Patched file exceeds max_write_bytes")
            _ensure_writable(p)
            p.write_text(updated, encoding="utf-8")
            app.audit.write("fs_patch_text", target=str(p), detail={"replacements": count})
            return ok("fs_patch_text", {"path": str(p), "replacements": count})
        except Exception as e:
            return fail("fs_patch_text", e)

    @mcp.tool()
    def fs_search(path: str, query: str, file_glob: str = "*", case_sensitive: bool = False, limit: int = 200) -> dict:
        """Search text across files locally and return compact matches. Read-only."""
        try:
            root = Path(app.policy.assert_path(path))
            needle = query if case_sensitive else query.lower()
            matches = []
            scanned = 0
            read_files = 0
            timed_out = False
            started = time.monotonic()
            max_matches = max(1, min(limit, 2000))
            for p in root.rglob("*"):
                scanned += 1
                if time.monotonic() - started >= FILESYSTEM_TRAVERSAL_BUDGET_SECONDS:
                    timed_out = True
                    break
                if not p.is_file() or not fnmatch.fnmatch(p.name, file_glob):
                    continue
                try:
                    app.policy.assert_path(str(p))
                    if p.stat().st_size > app.settings.max_read_bytes:
                        continue
                    lines = p.read_text(encoding="utf-8", errors="ignore").splitlines()
                    read_files += 1
                except (OSError, PolicyError):
                    continue
                for i, line in enumerate(lines, 1):
                    hay = line if case_sensitive else line.lower()
                    if needle in hay:
                        matches.append({"path": str(p), "line": i, "text": line[:1000]})
                        if len(matches) >= max_matches:
                            app.audit.write(
                                "fs_search",
                                target=str(root),
                                detail={
                                    "scanned": scanned,
                                    "read_files": read_files,
                                    "matches": len(matches),
                                    "timed_out": False,
                                },
                            )
                            return ok(
                                "fs_search",
                                {
                                    "matches": matches,
                                    "truncated": True,
                                    "timed_out": False,
                                    "scanned": scanned,
                                    "read_files": read_files,
                                },
                            )
            warnings = []
            if timed_out:
                warnings.append(
                    f"recursive search stopped after {FILESYSTEM_TRAVERSAL_BUDGET_SECONDS}s"
                )
            app.audit.write(
                "fs_search",
                target=str(root),
                detail={
                    "scanned": scanned,
                    "read_files": read_files,
                    "matches": len(matches),
                    "timed_out": timed_out,
                },
            )
            return ok(
                "fs_search",
                {
                    "matches": matches,
                    "truncated": timed_out,
                    "timed_out": timed_out,
                    "scanned": scanned,
                    "read_files": read_files,
                },
                warnings,
            )
        except Exception as e:
            return fail("fs_search", e)

    @mcp.tool()
    def fs_hash(path: str, algorithm: str = "sha256") -> dict:
        """Hash a file with sha256/sha1/md5 for integrity checks. Read-only."""
        try:
            p = Path(app.policy.assert_path(path))
            if algorithm not in {"sha256", "sha1", "md5"}:
                raise ValueError("algorithm must be sha256, sha1, or md5")
            h = hashlib.new(algorithm)
            with p.open("rb") as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b""):
                    h.update(chunk)
            return ok("fs_hash", {"path": str(p), "algorithm": algorithm, "digest": h.hexdigest()})
        except Exception as e:
            return fail("fs_hash", e)

    @mcp.tool()
    def fs_copy(src: str, dst: str, overwrite: bool = False) -> dict:
        """Copy a file or directory between allowed roots. Write action."""
        try:
            s = Path(app.policy.assert_path(src))
            d = Path(app.policy.assert_path(dst, write=True))
            if d.exists() and not overwrite:
                raise FileExistsError(str(d))
            if s.is_dir():
                if d == s or s in d.parents:
                    raise ValueError("Directory copy destination cannot be inside its source")
                _assert_safe_copy_tree(s)
                if d.exists():
                    _assert_safe_copy_tree(d)
                # Validate all descendants before writing any destination data.
                for current, dirs, files in os.walk(s, followlinks=False):
                    for name in [*dirs, *files]:
                        child = Path(current) / name
                        app.policy.assert_path(str(child))
                        app.policy.assert_path(str(d / child.relative_to(s)), write=True)
                shutil.copytree(s, d, dirs_exist_ok=overwrite, copy_function=_copy2_readonly_retry)
            else:
                if d.is_dir():
                    d = Path(app.policy.assert_path(str(d / s.name), write=True))
                    if d.exists() and not overwrite:
                        raise FileExistsError(str(d))
                d.parent.mkdir(parents=True, exist_ok=True)
                if d.exists() and overwrite:
                    _ensure_writable(d)
                _copy2_readonly_retry(str(s), str(d))
            app.audit.write("fs_copy", target=f"{s} -> {d}")
            return ok("fs_copy", {"src": str(s), "dst": str(d)})
        except Exception as e:
            return fail("fs_copy", e)

    @mcp.tool()
    def fs_exists(path: str) -> dict:
        """Check whether a path exists inside an allowed root. Read-only."""
        try:
            p = Path(app.policy.assert_path(path))
            return ok("fs_exists", {"path": str(p), "exists": p.exists()})
        except Exception as e:
            return fail("fs_exists", e)

    @mcp.tool()
    def fs_mkdir(path: str, parents: bool = True, exist_ok: bool = True) -> dict:
        """Create a directory inside an allowed root. Write action."""
        try:
            p = Path(app.policy.assert_path(path, write=True))
            p.mkdir(parents=parents, exist_ok=exist_ok)
            app.audit.write("fs_mkdir", target=str(p))
            return ok("fs_mkdir", {"path": str(p)})
        except Exception as e:
            return fail("fs_mkdir", e)

    @mcp.tool()
    def fs_move(src: str, dst: str, overwrite: bool = False) -> dict:
        """Move/rename a file or directory between allowed roots. Write action."""
        try:
            s = Path(app.policy.assert_path(src, write=True))
            d = Path(app.policy.assert_path(dst, write=True))
            if s == d or s in d.parents or d in s.parents:
                raise ValueError("Move source and destination cannot overlap")
            if any(str(s) == app.policy.normalize_path(root) for root in app.settings.allowed_roots):
                raise ValueError("Refusing to move an allowed root itself")
            if s.is_dir():
                _assert_tree_policy(app, s, destructive=False)
            if d.exists():
                if not overwrite:
                    raise FileExistsError(str(d))
                app.policy.assert_path(str(d), write=True, destructive=True)
                if d.is_dir():
                    _assert_tree_policy(app, d, destructive=True)
                    shutil.rmtree(d, onerror=_rmtree_onerror)
                else:
                    _unlink_readonly(d)
            d.parent.mkdir(parents=True, exist_ok=True)
            result = shutil.move(str(s), str(d))
            app.audit.write("fs_move", target=f"{s} -> {d}")
            return ok("fs_move", {"src": str(s), "dst": str(d), "result": result})
        except Exception as e:
            return fail("fs_move", e)

    @mcp.tool()
    def fs_delete(path: str, recursive: bool = False) -> dict:
        """Delete an allowed path. Destructive action and disabled by default."""
        try:
            p = Path(app.policy.assert_path(path, write=True, destructive=True))
            if p.is_dir():
                if recursive:
                    _assert_tree_policy(app, p, destructive=True)
                    shutil.rmtree(p, onerror=_rmtree_onerror)
                else:
                    p.rmdir()
            else:
                _unlink_readonly(p)
            app.audit.write("fs_delete", target=str(p))
            return ok("fs_delete", {"path": str(p)})
        except Exception as e:
            return fail("fs_delete", e)
