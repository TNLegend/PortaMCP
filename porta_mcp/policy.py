from __future__ import annotations

import os
import re
from pathlib import Path

from .audit import AuditLogger
from .config import Settings


class PolicyError(PermissionError):
    pass


class Policy:
    DANGEROUS_SHELL = re.compile(
        r"(?i)("
        r"format\s+[a-z]:|diskpart|bcdedit|cipher\s+/w|"
        r"Remove-Item\s+.*-Recurse|rd\s+/s|del\s+/[fsq]|"
        r"shutdown\s|Restart-Computer|Stop-Computer|"
        r"\b(?:rm|rmdir|unlink|shred|wipefs|fdisk|sfdisk|parted)\b|"
        r"\bmkfs(?:\.[a-z0-9_-]+)?\b|"
        r"\bdd\b[^\r\n]*\bof\s*=\s*/dev/|"
        r"\bfind\b[^\r\n]*\s-delete\b|"
        r"\b(?:shutdown|reboot|poweroff|halt)\b|"
        r"\bsystemctl\s+(?:stop|disable|mask|reboot|poweroff)\b"
        r")"
    )
    ADMIN_HINTS = re.compile(
        r"(?i)("
        r"runas|Start-Process\s+.*-Verb\s+RunAs|Set-MpPreference|"
        r"sc\.exe\s+config|reg\s+add\s+HKLM|"
        r"\b(?:sudo|doas|pkexec)\b|"
        r"(?:^|[;&|]\s*)su(?:\s|$)|"
        r"\bsystemctl\s+(?:start|stop|restart|enable|disable|mask|unmask|daemon-reload)\b|"
        r"\b(?:apt|apt-get|dnf|yum|pacman|zypper|apk)\s+"
        r"(?:install|remove|erase|upgrade|update|dist-upgrade|full-upgrade)\b|"
        r"\b(?:mount|umount|modprobe|insmod|rmmod|iptables|nft|"
        r"useradd|userdel|usermod|groupadd|groupdel|passwd|visudo)\b"
        r")"
    )

    def __init__(self, settings: Settings, audit: AuditLogger) -> None:
        self.settings = settings
        self.audit = audit

    def assert_not_stopped(self) -> None:
        if Path(self.settings.emergency_stop_file).exists():
            raise PolicyError("Emergency stop is active. Remove the stop file locally to re-enable tools.")

    def normalize_path(self, path: str) -> str:
        expanded = os.path.abspath(os.path.expandvars(os.path.expanduser(path)))
        return os.path.normcase(os.path.realpath(expanded))

    @staticmethod
    def _within(child: str, parent: str) -> bool:
        try:
            return os.path.commonpath([child, parent]) == os.path.commonpath([parent])
        except ValueError:
            return False

    def assert_path(self, path: str, *, write: bool = False, destructive: bool = False) -> str:
        self.assert_not_stopped()
        p = self.normalize_path(path)
        for denied in self.settings.denied_roots:
            if self._within(p, self.normalize_path(denied)):
                self.audit.write("path", target=p, allowed=False, detail="denied root")
                raise PolicyError(f"Path is in a denied root: {p}")
        allowed_roots = [self.normalize_path(root) for root in self.settings.allowed_roots]
        if not any(self._within(p, root) for root in allowed_roots):
            self.audit.write("path", target=p, allowed=False, detail="outside allowed roots")
            raise PolicyError(f"Path is outside allowed_roots: {p}")
        if write and not self.settings.allow_fs_write:
            self.audit.write("path", target=p, allowed=False, detail="filesystem writes disabled")
            raise PolicyError("Filesystem write operations are disabled by configuration.")
        if destructive and p in allowed_roots:
            self.audit.write("path", target=p, allowed=False, detail="protected allowed root")
            raise PolicyError("Refusing destructive operation on an allowed root itself.")
        if destructive and not self.settings.allow_destructive_fs:
            raise PolicyError("Destructive filesystem operations are disabled by configuration.")
        self.audit.write("path_write" if write else "path_read", target=p, allowed=True)
        return p

    def assert_shell(self, command: str) -> None:
        self.assert_not_stopped()
        if not self.settings.allow_shell:
            raise PolicyError("Shell execution is disabled.")
        if self.DANGEROUS_SHELL.search(command) and not self.settings.allow_destructive_fs:
            raise PolicyError("Potentially destructive shell command blocked by policy.")
        if self.ADMIN_HINTS.search(command) and not self.settings.allow_admin_commands:
            raise PolicyError("Administrative shell command blocked by policy.")
        self.audit.write("shell", allowed=True, detail={"command_chars": len(command)})

    def assert_feature(self, feature: str) -> None:
        self.assert_not_stopped()
        attr = {
            "input": "allow_input_control",
            "ui": "allow_ui_automation",
            "browser": "allow_browser_control",
            "process_start": "allow_process_start",
            "process_kill": "allow_process_kill",
        }.get(feature)
        if not attr:
            raise PolicyError(f"Unknown feature: {feature}")
        if not getattr(self.settings, attr):
            raise PolicyError(f"Feature '{feature}' is disabled by configuration.")
        self.audit.write("feature", target=feature, allowed=True)
