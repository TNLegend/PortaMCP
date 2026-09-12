from __future__ import annotations

import asyncio
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ShellSession:
    id: str
    cwd: str
    env: dict[str, str] = field(default_factory=dict)
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)


class RuntimeState:
    def __init__(self) -> None:
        self.shell_sessions: dict[str, ShellSession] = {}
        self.browser: Any = None
        self.playwright: Any = None
        self.browser_context: Any = None
        self.pages: dict[str, Any] = {}
        self.chrome_bridge: Any = None
        self.lock = threading.RLock()
        # Managed Playwright state is shared by all MCP requests in this
        # process. Serialize browser operations so lifecycle actions cannot
        # close/replace the browser while a sibling request is using it.
        self.browser_lock = asyncio.Lock()

    def new_shell_session(self, cwd: str) -> ShellSession:
        session = ShellSession(str(uuid.uuid4()), cwd)
        with self.lock:
            self.shell_sessions[session.id] = session
        return session

    def get_shell_session(self, session_id: str) -> ShellSession:
        with self.lock:
            session = self.shell_sessions.get(session_id)
        if session is None:
            raise KeyError(f"Unknown shell session: {session_id}")
        return session

    def close_shell_session(self, session_id: str) -> None:
        with self.lock:
            self.shell_sessions.pop(session_id, None)
