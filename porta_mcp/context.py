from __future__ import annotations

from dataclasses import dataclass

from .audit import AuditLogger
from .config import Settings
from .policy import Policy
from .state import RuntimeState


@dataclass(slots=True)
class AppContext:
    settings: Settings
    audit: AuditLogger
    policy: Policy
    state: RuntimeState


def build_context(settings: Settings) -> AppContext:
    settings.ensure_runtime_dirs()
    audit = AuditLogger(settings.audit_log_path, settings.enable_audit_log)
    return AppContext(settings, audit, Policy(settings, audit), RuntimeState())
