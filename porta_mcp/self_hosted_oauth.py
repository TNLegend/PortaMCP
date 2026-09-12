from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import logging
import os
import re
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import (
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)

LOGGER = logging.getLogger("porta_mcp.oauth")

SUPPORTED_SCOPE = "pc:control"
OFFLINE_SCOPE = "offline_access"
SUPPORTED_SCOPES = {SUPPORTED_SCOPE, OFFLINE_SCOPE}
DEFAULT_SCOPE = "pc:control offline_access"
MAX_REGISTRATION_BYTES = 64 * 1024
MAX_DYNAMIC_CLIENTS = 256
MAX_OAUTH_FORM_BYTES = 64 * 1024
MAX_PENDING_AUTHORIZATIONS = 1024
ACCESS_TOKEN_CLOCK_SKEW_SECONDS = 60
REFRESH_REPLAY_GRACE_SECONDS = 600
POST_TOKEN_AUTH_GRACE_SECONDS = 15


def _now() -> int:
    return int(time.time())


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _random_token(nbytes: int = 48) -> str:
    return secrets.token_urlsafe(nbytes)


def _pkce_s256(verifier: str) -> str | None:
    if not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier):
        return None
    try:
        raw = verifier.encode("ascii")
    except UnicodeEncodeError:
        return None
    digest = hashlib.sha256(raw).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _parse_form_bytes(raw: bytes) -> dict[str, str]:
    parsed = parse_qs(raw.decode("utf-8", errors="replace"), keep_blank_values=True)
    return {key: values[-1] if values else "" for key, values in parsed.items()}


async def _read_form(request: Request) -> dict[str, str]:
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_OAUTH_FORM_BYTES:
            raise HTTPException(status_code=413, detail="OAuth request too large")
        body.extend(chunk)
    return _parse_form_bytes(bytes(body))


def _is_safe_redirect_uri(uri: str) -> bool:
    if "\\" in uri or "#" in uri or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in uri):
        return False
    try:
        p = urlparse(uri)
        if not p.hostname or (p.port is not None and not 1 <= p.port <= 65535):
            return False
    except Exception:
        return False
    if p.fragment or p.username is not None or p.password is not None:
        return False
    if p.scheme == "https" and bool(p.netloc):
        return True
    if p.scheme == "http" and p.hostname in {"127.0.0.1", "localhost", "::1"}:
        return True
    return False


def _normalize_base_url(url: str) -> str:
    return url.strip().rstrip("/")


@dataclass(slots=True)
class PendingAuthorization:
    pending_id: str
    client_id: str
    redirect_uri: str
    state: str | None
    scope: str
    code_challenge: str
    resource: str | None
    created_at: int
    client_name: str


class OAuthStateStore:
    """Small persistent OAuth store for one-owner personal MCP deployments.

    Client registrations and token *hashes* survive server restarts. Raw access and
    refresh tokens are never written to disk. Authorization codes are short lived and
    kept in memory only.
    """

    def __init__(self, path: str, *, issuer: str, resource_url: str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.issuer = issuer
        self.resource_url = resource_url
        self._lock = threading.RLock()
        self.clients: dict[str, dict[str, Any]] = {}
        self.access_tokens: dict[str, dict[str, Any]] = {}
        self.refresh_tokens: dict[str, dict[str, Any]] = {}
        # Short-lived replay markers for just-rotated refresh tokens.
        # Only token hashes + metadata are persisted. Raw replay results stay
        # memory-only, so a restart can recover a lost refresh response without
        # writing raw access/refresh tokens to disk.
        self.refresh_replays: dict[str, dict[str, Any]] = {}
        self.codes: dict[str, dict[str, Any]] = {}
        self.pending: dict[str, PendingAuthorization] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            stored_issuer = _normalize_base_url(str(data.get("issuer") or ""))
            stored_resource = _normalize_base_url(str(data.get("resource_url") or ""))
            if stored_issuer != _normalize_base_url(self.issuer) or stored_resource != _normalize_base_url(self.resource_url):
                # Endpoint identity changed. Do not resurrect credentials issued for another public URL.
                self.clients = {}
                self.access_tokens = {}
                self.refresh_tokens = {}
                self.refresh_replays = {}
                self._save()
                return
            self.clients = dict(data.get("clients") or {})
            self.access_tokens = dict(data.get("access_tokens") or {})
            self.refresh_tokens = dict(data.get("refresh_tokens") or {})
            self.refresh_replays = {
                str(key): dict(value)
                for key, value in dict(data.get("refresh_replays") or {}).items()
                if isinstance(value, dict)
            }
        except Exception:
            # Fail closed on corrupt state rather than trusting partial credentials.
            self.clients = {}
            self.access_tokens = {}
            self.refresh_tokens = {}
            self.refresh_replays = {}

    def _save(self) -> None:
        data = {
            "version": 1,
            "issuer": self.issuer,
            "resource_url": self.resource_url,
            "clients": self.clients,
            "access_tokens": self.access_tokens,
            "refresh_tokens": self.refresh_tokens,
            "refresh_replays": {
                key: {k: v for k, v in record.items() if k != "result"}
                for key, record in self.refresh_replays.items()
            },
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)

    def cleanup(self) -> None:
        now = _now()
        with self._lock:
            changed = False
            expired_access = [
                k for k, v in self.access_tokens.items()
                if int(v.get("expires_at") or 0) + ACCESS_TOKEN_CLOCK_SKEW_SECONDS <= now
            ]
            for key in expired_access:
                self.access_tokens.pop(key, None)
                changed = True
            expired_refresh = [
                k for k, v in self.refresh_tokens.items()
                if int(v.get("expires_at") or 0) <= now
            ]
            for key in expired_refresh:
                self.refresh_tokens.pop(key, None)
                changed = True
            expired_replays = [
                k for k, v in self.refresh_replays.items()
                if int(v.get("expires_at") or 0) <= now
            ]
            for key in expired_replays:
                self.refresh_replays.pop(key, None)
            expired_codes = [k for k, v in self.codes.items() if int(v.get("expires_at") or 0) <= now]
            for key in expired_codes:
                self.codes.pop(key, None)
            expired_pending = [
                k for k, v in self.pending.items() if v.created_at + 600 <= now
            ]
            for key in expired_pending:
                self.pending.pop(key, None)
            if changed:
                self._save()

    def register_client(self, metadata: dict[str, Any]) -> dict[str, Any]:
        redirect_uris = metadata.get("redirect_uris") or []
        if not isinstance(redirect_uris, list) or not redirect_uris:
            raise ValueError("redirect_uris must be a non-empty list")
        if not all(isinstance(uri, str) and _is_safe_redirect_uri(uri) for uri in redirect_uris):
            raise ValueError("redirect_uris must use HTTPS, except localhost loopback HTTP")

        requested_scope = str(metadata.get("scope") or DEFAULT_SCOPE).strip()
        requested_scopes = set(filter(None, requested_scope.split()))
        if SUPPORTED_SCOPE not in requested_scopes or not requested_scopes.issubset(SUPPORTED_SCOPES):
            raise ValueError("unsupported scope")

        auth_method = str(metadata.get("token_endpoint_auth_method") or "none")
        if auth_method not in {"none", "client_secret_post", "client_secret_basic"}:
            raise ValueError("unsupported token_endpoint_auth_method")

        client_id = "mcp_" + secrets.token_urlsafe(24)
        client_secret = None if auth_method == "none" else secrets.token_urlsafe(36)
        record = dict(metadata)
        record.update(
            {
                "client_id": client_id,
                "client_id_issued_at": _now(),
                "token_endpoint_auth_method": auth_method,
                "scope": " ".join(sorted(requested_scopes)),
            }
        )
        if client_secret:
            record["client_secret"] = client_secret
        else:
            record.pop("client_secret", None)

        with self._lock:
            # Repeated connector setup attempts commonly repeat dynamic
            # registration for the same public client. Reuse an equivalent
            # public registration instead of accumulating stale client IDs.
            if auth_method == "none":
                fingerprint = {
                    "client_name": str(record.get("client_name") or ""),
                    "redirect_uris": sorted(str(x) for x in redirect_uris),
                    "grant_types": sorted(str(x) for x in (record.get("grant_types") or [])),
                    "response_types": sorted(str(x) for x in (record.get("response_types") or [])),
                    "scope": str(record.get("scope") or ""),
                    "token_endpoint_auth_method": "none",
                }
                for existing in self.clients.values():
                    if str(existing.get("token_endpoint_auth_method") or "none") != "none":
                        continue
                    existing_fingerprint = {
                        "client_name": str(existing.get("client_name") or ""),
                        "redirect_uris": sorted(str(x) for x in (existing.get("redirect_uris") or [])),
                        "grant_types": sorted(str(x) for x in (existing.get("grant_types") or [])),
                        "response_types": sorted(str(x) for x in (existing.get("response_types") or [])),
                        "scope": str(existing.get("scope") or ""),
                        "token_endpoint_auth_method": "none",
                    }
                    if existing_fingerprint == fingerprint:
                        return dict(existing)

            if len(self.clients) >= MAX_DYNAMIC_CLIENTS:
                raise ValueError("dynamic client registration limit reached")
            self.clients[client_id] = record
            self._save()
        return record

    def get_client(self, client_id: str) -> dict[str, Any] | None:
        with self._lock:
            value = self.clients.get(client_id)
            return dict(value) if value else None

    def authenticate_client(self, form: dict[str, str], authorization: str | None) -> dict[str, Any] | None:
        client_id = form.get("client_id", "")
        client_secret = form.get("client_secret")
        if authorization and authorization.lower().startswith("basic "):
            try:
                raw = base64.b64decode(authorization.split(" ", 1)[1]).decode("utf-8")
                basic_id, basic_secret = raw.split(":", 1)
                client_id = basic_id
                client_secret = basic_secret
            except Exception:
                return None
        client = self.get_client(client_id)
        if not client:
            return None
        method = client.get("token_endpoint_auth_method") or "none"
        if method == "none":
            return client
        expected = str(client.get("client_secret") or "")
        if not expected or client_secret is None or not hmac.compare_digest(expected.encode("utf-8"), client_secret.encode("utf-8")):
            return None
        return client

    def create_pending(
        self,
        *,
        client: dict[str, Any],
        redirect_uri: str,
        state: str | None,
        scope: str,
        code_challenge: str,
        resource: str | None,
    ) -> PendingAuthorization:
        self.cleanup()
        pending = PendingAuthorization(
            pending_id=secrets.token_urlsafe(32),
            client_id=str(client["client_id"]),
            redirect_uri=redirect_uri,
            state=state,
            scope=scope,
            code_challenge=code_challenge,
            resource=resource,
            created_at=_now(),
            client_name=str(client.get("client_name") or "MCP client"),
        )
        with self._lock:
            if len(self.pending) >= MAX_PENDING_AUTHORIZATIONS:
                raise ValueError("pending authorization limit reached")
            self.pending[pending.pending_id] = pending
        return pending

    def get_pending(self, pending_id: str) -> PendingAuthorization | None:
        with self._lock:
            return self.pending.get(pending_id)

    def pop_pending(self, pending_id: str) -> PendingAuthorization | None:
        with self._lock:
            return self.pending.pop(pending_id, None)

    def issue_authorization_code(self, pending: PendingAuthorization) -> str:
        code = secrets.token_urlsafe(32)
        with self._lock:
            self.codes[code] = {
                "client_id": pending.client_id,
                "redirect_uri": pending.redirect_uri,
                "scope": pending.scope,
                "code_challenge": pending.code_challenge,
                "resource": pending.resource or self.resource_url,
                "expires_at": _now() + 300,
            }
        return code

    def exchange_code(
        self,
        *,
        client: dict[str, Any],
        code: str,
        redirect_uri: str,
        code_verifier: str,
        access_ttl: int,
        refresh_ttl: int,
    ) -> dict[str, Any] | None:
        with self._lock:
            record = self.codes.pop(code, None)
            if not record:
                return None
            if int(record["expires_at"]) <= _now():
                return None
            if str(record["client_id"]) != str(client["client_id"]):
                return None
            if str(record["redirect_uri"]) != redirect_uri:
                return None
            pkce = _pkce_s256(code_verifier) if code_verifier else None
            if pkce is None or not hmac.compare_digest(str(record["code_challenge"]).encode("utf-8"), pkce.encode("ascii")):
                return None
            return self._mint_pair_locked(
                client_id=str(client["client_id"]),
                scope=str(record["scope"]),
                resource=str(record.get("resource") or self.resource_url),
                access_ttl=access_ttl,
                refresh_ttl=refresh_ttl,
            )

    def _mint_pair_locked(
        self, *, client_id: str, scope: str, resource: str, access_ttl: int, refresh_ttl: int
    ) -> dict[str, Any]:
        now = _now()
        access = _random_token()
        refresh = _random_token()
        self.access_tokens[_token_hash(access)] = {
            "client_id": client_id,
            "scope": scope,
            "resource": resource,
            "expires_at": now + access_ttl,
        }
        self.refresh_tokens[_token_hash(refresh)] = {
            "client_id": client_id,
            "scope": scope,
            "resource": resource,
            "expires_at": now + refresh_ttl,
        }
        self._save()
        return {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": access_ttl,
            "refresh_token": refresh,
            "scope": scope,
        }

    def exchange_refresh(
        self,
        *,
        client: dict[str, Any],
        refresh_token: str,
        requested_scope: str | None,
        access_ttl: int,
        refresh_ttl: int,
    ) -> dict[str, Any] | None:
        """Rotate a refresh token with a short idempotent replay window.

        Concurrent MCP clients can submit the same refresh token at nearly the
        same time. The first request rotates it normally. For a brief in-memory
        grace window, duplicate requests receive the exact same token response
        instead of an invalid_grant HTTP 400 that could cancel sibling
        Streamable HTTP calls. Raw replay tokens are never persisted to disk.
        """
        key = _token_hash(refresh_token)
        now = _now()
        with self._lock:
            replay = self.refresh_replays.get(key)
            if replay is not None:
                if int(replay.get("expires_at") or 0) <= now:
                    self.refresh_replays.pop(key, None)
                    self._save()
                elif str(replay.get("client_id") or "") == str(client["client_id"]):
                    replay_scope = str(replay.get("scope") or "")
                    requested = " ".join(sorted((requested_scope or replay_scope).split()))
                    if requested != replay_scope:
                        return None

                    # Same-process duplicate: return the exact response.
                    if isinstance(replay.get("result"), dict):
                        return dict(replay["result"])

                    # Restart recovery: only the old token hash + metadata were
                    # persisted. Mint a fresh pair rather than invalid_grant.
                    result = self._mint_pair_locked(
                        client_id=str(client["client_id"]),
                        scope=replay_scope,
                        resource=str(replay.get("resource") or self.resource_url),
                        access_ttl=access_ttl,
                        refresh_ttl=refresh_ttl,
                    )
                    replay["expires_at"] = now + REFRESH_REPLAY_GRACE_SECONDS
                    replay["resource"] = str(replay.get("resource") or self.resource_url)
                    replay["result"] = dict(result)
                    replay["access_hash"] = _token_hash(result["access_token"])
                    replay["refresh_hash"] = _token_hash(result["refresh_token"])
                    self._save()
                    return result
                else:
                    return None

            record = self.refresh_tokens.pop(key, None)
            if not record or int(record["expires_at"]) <= now:
                return None
            if str(record["client_id"]) != str(client["client_id"]):
                self.refresh_tokens[key] = record
                return None
            granted = set(str(record["scope"]).split())
            requested_set = set((requested_scope or str(record["scope"])).split())
            if not requested_set.issubset(granted):
                self.refresh_tokens[key] = record
                return None

            scope = " ".join(sorted(requested_set))
            result = self._mint_pair_locked(
                client_id=str(client["client_id"]),
                scope=scope,
                resource=str(record.get("resource") or self.resource_url),
                access_ttl=access_ttl,
                refresh_ttl=refresh_ttl,
            )
            self.refresh_replays[key] = {
                "client_id": str(client["client_id"]),
                "scope": scope,
                "resource": str(record.get("resource") or self.resource_url),
                "expires_at": now + REFRESH_REPLAY_GRACE_SECONDS,
                "result": dict(result),
                "access_hash": _token_hash(result["access_token"]),
                "refresh_hash": _token_hash(result["refresh_token"]),
            }
            self._save()
            return result

    def verify_access_token(self, token: str, required_scope: str = SUPPORTED_SCOPE) -> dict[str, Any] | None:
        self.cleanup()
        with self._lock:
            record = self.access_tokens.get(_token_hash(token))
            if not record or int(record["expires_at"]) + ACCESS_TOKEN_CLOCK_SKEW_SECONDS <= _now():
                return None
            if required_scope not in str(record.get("scope") or "").split():
                return None
            if _normalize_base_url(str(record.get("resource") or "")) != _normalize_base_url(self.resource_url):
                return None
            return dict(record)

    def revoke(self, token: str) -> None:
        key = _token_hash(token)
        with self._lock:
            self.access_tokens.pop(key, None)
            self.refresh_tokens.pop(key, None)
            # A revoked refresh must not be resurrected by the reconnect cache.
            for old_key, replay in list(self.refresh_replays.items()):
                result = replay.get("result") or {}
                access_hash = replay.get("access_hash") or (_token_hash(result["access_token"]) if result else None)
                refresh_hash = replay.get("refresh_hash") or (_token_hash(result["refresh_token"]) if result else None)
                if key in {old_key, refresh_hash}:
                    self.refresh_replays.pop(old_key, None)
                    self.access_tokens.pop(access_hash, None)
                    self.refresh_tokens.pop(refresh_hash, None)
            self._save()


class LoginThrottle:
    def __init__(self, max_failures: int = 8, window_seconds: int = 300) -> None:
        self.max_failures = max_failures
        self.window_seconds = window_seconds
        self.failures: dict[str, list[int]] = {}
        self.lock = threading.Lock()

    def allowed(self, key: str) -> bool:
        now = _now()
        with self.lock:
            xs = [x for x in self.failures.get(key, []) if x + self.window_seconds > now]
            self.failures[key] = xs
            return len(xs) < self.max_failures

    def fail(self, key: str) -> None:
        with self.lock:
            self.failures.setdefault(key, []).append(_now())

    def success(self, key: str) -> None:
        with self.lock:
            self.failures.pop(key, None)


class SelfHostedOAuth:
    def __init__(
        self,
        *,
        public_base_url: str,
        state_path: str,
        owner_password_file: str,
        access_ttl: int = 3600,
        refresh_ttl: int = 2_592_000,
    ) -> None:
        self.base_url = _normalize_base_url(public_base_url)
        if not self.base_url.startswith("https://"):
            raise ValueError("public_base_url must be HTTPS for remote OAuth")
        self.resource_url = self.base_url + "/mcp"
        self.state = OAuthStateStore(state_path, issuer=self.base_url, resource_url=self.resource_url)
        self.password_file = Path(owner_password_file)
        self.access_ttl = int(access_ttl)
        self.refresh_ttl = int(refresh_ttl)
        self.throttle = LoginThrottle()
        self._token_issue_lock = threading.Lock()
        self._token_issue_grace_until = 0.0

    def note_token_issued(self) -> None:
        with self._token_issue_lock:
            self._token_issue_grace_until = (
                time.monotonic() + POST_TOKEN_AUTH_GRACE_SECONDS
            )

    def token_was_just_issued(self) -> bool:
        with self._token_issue_lock:
            return time.monotonic() < self._token_issue_grace_until

    def _owner_password(self) -> str:
        try:
            return self.password_file.read_text(encoding="utf-8").strip()
        except FileNotFoundError as exc:
            raise RuntimeError(f"OAuth owner password file is missing: {self.password_file}") from exc

    def authorization_server_metadata(self) -> dict[str, Any]:
        return {
            "issuer": self.base_url,
            "authorization_endpoint": self.base_url + "/authorize",
            "token_endpoint": self.base_url + "/token",
            "registration_endpoint": self.base_url + "/register",
            "revocation_endpoint": self.base_url + "/revoke",
            "scopes_supported": [SUPPORTED_SCOPE, OFFLINE_SCOPE],
            "response_types_supported": ["code"],
            "response_modes_supported": ["query"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": ["none", "client_secret_post", "client_secret_basic"],
            "code_challenge_methods_supported": ["S256"],
            "authorization_response_iss_parameter_supported": False,
        }

    def protected_resource_metadata(self) -> dict[str, Any]:
        return {
            "resource": self.resource_url,
            "authorization_servers": [self.base_url],
            "scopes_supported": [SUPPORTED_SCOPE],
            "bearer_methods_supported": ["header"],
            "resource_name": "PortaMCP",
        }

    async def root_handler(self, request: Request) -> Response:
        return JSONResponse(
            {
                "ok": True,
                "service": "PortaMCP",
                "mcp_endpoint": "/mcp",
                "oauth_metadata": "/.well-known/oauth-authorization-server",
            },
            headers={"Cache-Control": "no-store"},
        )

    async def robots_handler(self, request: Request) -> Response:
        return PlainTextResponse(
            "User-agent: *\nDisallow: /\n",
            headers={"Cache-Control": "public, max-age=3600"},
        )

    async def favicon_handler(self, request: Request) -> Response:
        return Response(status_code=204, headers={"Cache-Control": "public, max-age=3600"})

    async def metadata_handler(self, request: Request) -> Response:
        return JSONResponse(self.authorization_server_metadata())

    async def protected_resource_handler(self, request: Request) -> Response:
        return JSONResponse(self.protected_resource_metadata())

    async def register_handler(self, request: Request) -> Response:
        try:
            if len(self.state.clients) >= MAX_DYNAMIC_CLIENTS:
                return JSONResponse({"error": "too_many_clients"}, status_code=429)
            content_length = request.headers.get("content-length")
            if content_length is not None and int(content_length) > MAX_REGISTRATION_BYTES:
                return JSONResponse({"error": "request_too_large"}, status_code=413)
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > MAX_REGISTRATION_BYTES:
                    return JSONResponse({"error": "request_too_large"}, status_code=413)
            metadata = json.loads(bytes(body).decode("utf-8"))
            if not isinstance(metadata, dict):
                raise ValueError("JSON object required")
            client = self.state.register_client(metadata)
            return JSONResponse(client, status_code=201, headers={"Cache-Control": "no-store"})
        except ValueError as exc:
            return JSONResponse(
                {"error": "invalid_client_metadata", "error_description": str(exc)}, status_code=400
            )
        except Exception:
            return JSONResponse({"error": "invalid_client_metadata"}, status_code=400)

    async def authorize_handler(self, request: Request) -> Response:
        q = request.query_params
        client_id = q.get("client_id", "")
        client = self.state.get_client(client_id)
        if not client:
            return JSONResponse(
                {"error": "unauthorized_client", "iss": self.base_url},
                status_code=400,
                headers={"Cache-Control": "no-store"},
            )
        redirect_uri = q.get("redirect_uri", "")
        if not redirect_uri or redirect_uri not in (client.get("redirect_uris") or []):
            return JSONResponse(
                {
                    "error": "invalid_request",
                    "error_description": "invalid redirect_uri",
                    "iss": self.base_url,
                },
                status_code=400,
                headers={"Cache-Control": "no-store"},
            )
        if q.get("response_type") != "code":
            return self._oauth_redirect(
                redirect_uri,
                error="unsupported_response_type",
                state=q.get("state"),
            )
        scope = (q.get("scope") or DEFAULT_SCOPE).strip()
        requested_scopes = set(filter(None, scope.split()))
        registered_scopes = set(str(client.get("scope") or DEFAULT_SCOPE).split())
        if (SUPPORTED_SCOPE not in requested_scopes or not requested_scopes.issubset(SUPPORTED_SCOPES)
                or not requested_scopes.issubset(registered_scopes)):
            return self._oauth_redirect(redirect_uri, error="invalid_scope", state=q.get("state"))
        code_challenge = q.get("code_challenge", "")
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", code_challenge) or q.get("code_challenge_method") != "S256":
            return self._oauth_redirect(
                redirect_uri,
                error="invalid_request",
                state=q.get("state"),
                error_description="PKCE S256 is required",
            )
        resource = q.get("resource")
        if resource and _normalize_base_url(resource) not in {
            _normalize_base_url(self.resource_url),
            _normalize_base_url(self.base_url),
        }:
            return self._oauth_redirect(redirect_uri, error="invalid_target", state=q.get("state"))

        try:
            pending = self.state.create_pending(
                client=client,
                redirect_uri=redirect_uri,
                state=q.get("state"),
                scope=scope,
                code_challenge=code_challenge,
                resource=self.resource_url,
            )
        except ValueError:
            return JSONResponse({"error": "temporarily_unavailable"}, status_code=429, headers={"Cache-Control": "no-store"})
        client_name = html.escape(pending.client_name)
        return HTMLResponse(
            f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Authorize PortaMCP</title>
<style>
body{{font-family:system-ui,-apple-system,Segoe UI,sans-serif;background:#111;color:#eee;display:grid;place-items:center;min-height:100vh;margin:0}}
.card{{max-width:560px;background:#1b1b1b;border:1px solid #444;border-radius:16px;padding:28px;box-shadow:0 18px 55px #0008}}
h1{{font-size:24px;margin-top:0}} .warn{{background:#3a210d;border:1px solid #9a5b22;padding:14px;border-radius:10px;line-height:1.45}}
label{{display:block;margin-top:18px;font-weight:600}} input{{width:100%;box-sizing:border-box;margin-top:8px;padding:12px;border-radius:9px;border:1px solid #555;background:#0e0e0e;color:#fff}}
.actions{{display:flex;gap:10px;margin-top:20px}} button{{padding:11px 16px;border:0;border-radius:9px;font-weight:700;cursor:pointer}} .allow{{background:#fff;color:#111}} .deny{{background:#333;color:#fff}}
small{{color:#aaa}}</style></head><body><div class="card">
<h1>Authorize PortaMCP</h1>
<p><b>{client_name}</b> is requesting access to PortaMCP.</p>
<div class="warn"><b>High-impact computer control:</b> if your PortaMCP configuration enables it, the client can run commands, modify files, control desktop input and applications, and control the browser.</div>
<form method="post" action="/authorize/decision">
<input type="hidden" name="pending_id" value="{html.escape(pending.pending_id)}">
<label>Owner password<input name="password" type="password" autocomplete="current-password" required autofocus></label>
<small>The password is stored only on your PC in <code>.portamcp/oauth_owner_password.txt</code>.</small>
<div class="actions"><button class="allow" name="decision" value="allow">Allow</button><button class="deny" name="decision" value="deny">Deny</button></div>
</form></div></body></html>""",
            headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"},
        )

    async def decision_handler(self, request: Request) -> Response:
        form = await _read_form(request)
        pending_id = form.get("pending_id", "")
        pending = self.state.get_pending(pending_id)
        if not pending or pending.created_at + 600 <= _now():
            self.state.pop_pending(pending_id)
            return HTMLResponse("Authorization request expired. Start the connection again.", status_code=400)
        if form.get("decision") == "deny":
            self.state.pop_pending(pending_id)
            return self._oauth_redirect(pending.redirect_uri, error="access_denied", state=pending.state)

        peer = request.client.host if request.client else "unknown"
        if not self.throttle.allowed(peer):
            return HTMLResponse(
                "Too many failed login attempts. Wait a few minutes and try again.",
                status_code=429,
                headers={"Cache-Control": "no-store"},
            )
        supplied = form.get("password", "")
        try:
            correct = self._owner_password()
        except RuntimeError as exc:
            return HTMLResponse(html.escape(str(exc)), status_code=500)
        if not supplied or not hmac.compare_digest(supplied.encode("utf-8"), correct.encode("utf-8")):
            self.throttle.fail(peer)
            # Keep the pending request alive so a typo does not destroy the
            # entire OAuth flow. The owner can correct the password and retry.
            return HTMLResponse(
                "Incorrect owner password. Correct it and submit again.",
                status_code=401,
                headers={"Cache-Control": "no-store"},
            )

        claimed = self.state.pop_pending(pending_id)
        if claimed is None:
            return HTMLResponse("Authorization request expired. Start the connection again.", status_code=400)
        self.throttle.success(peer)
        code = self.state.issue_authorization_code(claimed)
        return self._oauth_redirect(claimed.redirect_uri, code=code, state=claimed.state)

    def _oauth_redirect(
        self,
        redirect_uri: str,
        *,
        code: str | None = None,
        error: str | None = None,
        state: str | None = None,
        error_description: str | None = None,
    ) -> RedirectResponse:
        params: dict[str, str] = {}
        if code:
            params["code"] = code
        if error:
            params["error"] = error
        if error_description:
            params["error_description"] = error_description
        if state is not None:
            params["state"] = state
        sep = "&" if "?" in redirect_uri else "?"
        return RedirectResponse(redirect_uri + sep + urlencode(params), status_code=302)

    async def token_handler(self, request: Request) -> Response:
        form = await _read_form(request)
        client = self.state.authenticate_client(form, request.headers.get("authorization"))
        if not client:
            return JSONResponse(
                {"error": "invalid_client"},
                status_code=401,
                headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
            )
        resource = form.get("resource")
        if resource and _normalize_base_url(resource) != _normalize_base_url(self.resource_url):
            return self._token_error("invalid_target")

        grant = form.get("grant_type", "")
        if grant == "authorization_code":
            result = self.state.exchange_code(
                client=client,
                code=form.get("code", ""),
                redirect_uri=form.get("redirect_uri", ""),
                code_verifier=form.get("code_verifier", ""),
                access_ttl=self.access_ttl,
                refresh_ttl=self.refresh_ttl,
            )
            if not result:
                return self._token_error("invalid_grant")
            self.note_token_issued()
            return JSONResponse(result, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})
        if grant == "refresh_token":
            result = self.state.exchange_refresh(
                client=client,
                refresh_token=form.get("refresh_token", ""),
                requested_scope=form.get("scope") or None,
                access_ttl=self.access_ttl,
                refresh_ttl=self.refresh_ttl,
            )
            if not result:
                return self._token_error("invalid_grant")
            self.note_token_issued()
            return JSONResponse(result, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})
        return self._token_error("unsupported_grant_type")

    def _token_error(self, error: str, status: int = 400) -> JSONResponse:
        return JSONResponse(
            {"error": error},
            status_code=status,
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )

    async def revoke_handler(self, request: Request) -> Response:
        form = await _read_form(request)
        token = form.get("token", "")
        # Revocation is intentionally idempotent for this local personal OAuth
        # server. Possession of the token is sufficient to revoke it, and
        # unknown/already-revoked tokens still return 200. This avoids turning
        # reconnect cleanup into a client-fatal HTTP 401.
        if token:
            self.state.revoke(token)
        return Response(
            status_code=200,
            headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
        )

    async def health_handler(self, request: Request) -> Response:
        return JSONResponse(
            {
                "ok": True,
                "mode": "self-hosted-oauth",
                "issuer": self.base_url,
                "resource": self.resource_url,
                "scope": SUPPORTED_SCOPE,
            }
        )


class RootMcpAliasMiddleware:
    """Accept MCP POSTs at the public base URL without bypassing OAuth."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if (
            scope.get("type") == "http"
            and scope.get("path") == "/"
            and str(scope.get("method") or "").upper() in {"POST", "DELETE"}
        ):
            scope = dict(scope)
            scope["path"] = "/mcp"
            scope["raw_path"] = b"/mcp"
        await self.app(scope, receive, send)


class OAuthGateMiddleware:
    """Protect only the MCP transport path with OAuth bearer tokens."""

    def __init__(self, app: Any, oauth: SelfHostedOAuth) -> None:
        self.app = app
        self.oauth = oauth

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "http" and scope.get("path") == "/mcp":
            headers = {k.decode("latin1").lower(): v.decode("latin1") for k, v in scope.get("headers", [])}
            auth = headers.get("authorization", "")
            token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
            record = self.oauth.state.verify_access_token(token) if token else None
            if not record:
                method = str(scope.get("method") or "").upper()

                if (
                    not token
                    and method == "POST"
                    and self.oauth.token_was_just_issued()
                ):
                    LOGGER.warning(
                        "mcp_auth_rejected reason=post_token_missing_bearer method=POST"
                    )
                    body = bytearray()
                    more_body = True
                    while more_body:
                        message = await receive()
                        if message.get("type") != "http.request":
                            break
                        if len(body) + len(message.get("body", b"")) > MAX_OAUTH_FORM_BYTES:
                            response = JSONResponse({"error": "request_too_large"}, status_code=413)
                            await response(scope, receive, send)
                            return
                        body.extend(message.get("body", b""))
                        more_body = bool(message.get("more_body", False))

                    request_id: Any = None
                    try:
                        payload = json.loads(bytes(body).decode("utf-8"))
                        if isinstance(payload, dict):
                            request_id = payload.get("id")
                    except Exception:
                        request_id = None

                    response = JSONResponse(
                        {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "error": {
                                "code": -32001,
                                "message": "Authorization required",
                                "data": {"reauthenticate": True},
                            },
                        },
                        status_code=200,
                        headers={
                            "Cache-Control": "no-store",
                            "X-PortaMCP-Auth": "post-token-missing-bearer",
                        },
                    )
                    await response(scope, receive, send)
                    return

                if token and method == "POST":
                    LOGGER.warning("mcp_auth_rejected reason=stale_bearer method=POST")
                    # A stale/rotated bearer can race with a freshly issued
                    # access token during reconnect. Keep authorization
                    # fail-closed, but do not turn that stale sibling request
                    # into a transport-level 401 that can cancel the client's
                    # shared MCP TaskGroup.
                    body = bytearray()
                    more_body = True
                    while more_body:
                        message = await receive()
                        if message.get("type") != "http.request":
                            break
                        if len(body) + len(message.get("body", b"")) > MAX_OAUTH_FORM_BYTES:
                            response = JSONResponse({"error": "request_too_large"}, status_code=413)
                            await response(scope, receive, send)
                            return
                        body.extend(message.get("body", b""))
                        more_body = bool(message.get("more_body", False))

                    request_id: Any = None
                    try:
                        payload = json.loads(bytes(body).decode("utf-8"))
                        if isinstance(payload, dict):
                            request_id = payload.get("id")
                    except Exception:
                        request_id = None

                    response = JSONResponse(
                        {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "error": {
                                "code": -32001,
                                "message": "Authorization required",
                                "data": {"reauthenticate": True},
                            },
                        },
                        status_code=200,
                        headers={
                            "Cache-Control": "no-store",
                            "X-PortaMCP-Auth": "stale-bearer",
                        },
                    )
                    await response(scope, receive, send)
                    return

                LOGGER.warning(
                    "mcp_auth_rejected reason=%s method=%s",
                    "stale_bearer" if token else "missing_bearer",
                    str(scope.get("method") or ""),
                )
                response = JSONResponse(
                    {"error": "unauthorized"},
                    status_code=401,
                    headers={
                        "WWW-Authenticate": (
                            f'Bearer resource_metadata="{self.oauth.base_url}/.well-known/oauth-protected-resource/mcp", '
                            f'scope="{SUPPORTED_SCOPE}"'
                        ),
                        "Cache-Control": "no-store",
                    },
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)
