from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values


@dataclass(frozen=True)
class Settings:
    erpnext_url: str
    site: str | None = None
    api_key: str = field(default="", repr=False)
    api_secret: str = field(default="", repr=False)
    access_token: str = field(default="", repr=False)
    safe_mode: bool = True
    env_file: str | None = None
    env_file_mtime_ns: int | None = None
    state_dir: str = ".mcp-state"
    timeout: float = 30.0
    transport: str = "stdio"
    host: str = "127.0.0.1"
    port: int = 8089
    max_connections: int = 8
    max_keepalive_connections: int = 4
    keepalive_expiry: float = 30.0
    metadata_cache_ttl: float = 60.0
    metadata_cache_max_entries: int = 256
    plan_ttl: int = 3600
    inline_result_bytes: int = 12000
    max_artifact_bytes: int = 67108864
    max_state_bytes: int = 536870912
    ignored_legacy_keys: tuple[str, ...] = ()

    @property
    def identity(self) -> str:
        data = [self.erpnext_url, self.site, self.api_key, self.api_secret, self.access_token]
        return hashlib.sha256(json.dumps(data).encode()).hexdigest()

    @classmethod
    def from_env(cls) -> Settings:
        values, env_file = _load_env_values()
        url = values.get("ERPNEXT_URL", "").rstrip("/")
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("ERPNEXT_URL must be an HTTP(S) base URL without credentials/query")
        token = values.get("ERPNEXT_ACCESS_TOKEN", "")
        key, secret = values.get("ERPNEXT_API_KEY", ""), values.get("ERPNEXT_API_SECRET", "")
        if not token and not (key and secret):
            raise ValueError("Configure API key/secret or ERPNEXT_ACCESS_TOKEN")
        safe = values.get("safe_mode", values.get("SAFE_MODE", "1")).strip()
        if safe not in {"0", "1"}:
            raise ValueError("safe_mode must be 0 or 1")
        base = env_file.parent if env_file else Path.cwd()
        state = Path(values.get("ERPNEXT_STATE_DIR", ".mcp-state"))
        if not state.is_absolute():
            state = base / state
        ignored = tuple(
            sorted(
                k
                for k in values
                if k.startswith(
                    ("ERPNEXT_ALLOWED_", "ERPNEXT_READ_ALLOWED_", "ERPNEXT_DELETE_ALLOWED_")
                )
                or k in {"ERPNEXT_ALLOWLIST_FILE", "ERPNEXT_CLIENT_CACHE_MAX_TARGETS"}
            )
        )
        settings = cls(
            erpnext_url=url,
            site=values.get("ERPNEXT_SITE") or None,
            api_key=key,
            api_secret=secret,
            access_token=token,
            safe_mode=safe == "1",
            env_file=str(env_file) if env_file else None,
            env_file_mtime_ns=env_file.stat().st_mtime_ns if env_file else None,
            state_dir=str(state.resolve()),
            ignored_legacy_keys=ignored,
            timeout=_number(values, "ERPNEXT_TIMEOUT", 30),
            transport=values.get("MCP_TRANSPORT", "stdio"),
            host=values.get("MCP_HOST", "127.0.0.1"),
            port=_integer(values, "MCP_PORT", 8089),
            max_connections=_integer(values, "ERPNEXT_MAX_CONNECTIONS", 8),
            max_keepalive_connections=_integer(
                values, "ERPNEXT_MAX_KEEPALIVE_CONNECTIONS", 4, zero=True
            ),
            keepalive_expiry=_number(values, "ERPNEXT_KEEPALIVE_EXPIRY", 30, zero=True),
            metadata_cache_ttl=_number(values, "ERPNEXT_METADATA_CACHE_TTL", 60, zero=True),
            metadata_cache_max_entries=_integer(values, "ERPNEXT_METADATA_CACHE_MAX_ENTRIES", 256),
            plan_ttl=_integer(values, "ERPNEXT_PLAN_TTL", 3600),
            inline_result_bytes=_integer(values, "ERPNEXT_INLINE_RESULT_BYTES", 12000),
            max_artifact_bytes=_integer(values, "ERPNEXT_MAX_ARTIFACT_BYTES", 67108864),
            max_state_bytes=_integer(values, "ERPNEXT_MAX_STATE_BYTES", 536870912),
        )
        if settings.max_keepalive_connections > settings.max_connections:
            raise ValueError("Keepalive connections must not exceed max connections")
        if settings.inline_result_bytes < 1024:
            raise ValueError("ERPNEXT_INLINE_RESULT_BYTES must be at least 1024")
        return settings

    def public_status(self) -> dict:
        return {
            "url": self.erpnext_url,
            "site": self.site,
            "erpnext_url": self.erpnext_url,
            "env_file": self.env_file,
            "env_file_mtime_ns": self.env_file_mtime_ns,
            "safe_mode": int(self.safe_mode),
            "state_dir": self.state_dir,
            "authorization": "erpnext_permissions",
            "mcp_allowlist_enabled": False,
            "ignored_legacy_keys": list(self.ignored_legacy_keys),
            "confirmation_source": "agent_attestation",
            "plan_ttl_seconds": self.plan_ttl,
            "inline_result_bytes": self.inline_result_bytes,
            "max_artifact_bytes": self.max_artifact_bytes,
            "max_state_bytes": self.max_state_bytes,
            "transport": self.transport,
        }


def _number(values: dict, key: str, default: int, *, zero: bool = False) -> float:
    number = float(values.get(key, default))
    if not math.isfinite(number) or (number < 0 if zero else number <= 0):
        raise ValueError(f"{key} must be {'non-negative' if zero else 'positive'}")
    return number


def _integer(values: dict, key: str, default: int, *, zero: bool = False) -> int:
    number = _number(values, key, default, zero=zero)
    if not number.is_integer():
        raise ValueError(f"{key} must be an integer")
    return int(number)


def _load_env_values() -> tuple[dict[str, str], Path | None]:
    explicit = os.getenv("ERPNEXT_ENV_FILE")
    if explicit:
        candidate = Path(explicit).resolve()
        if not candidate.is_file():
            raise ValueError("ERPNEXT_ENV_FILE does not exist; refusing fallback to another target")
    else:
        candidate = next(
            (
                p
                for p in (Path(__file__).resolve().parents[2] / ".env", Path.cwd() / ".env")
                if p.is_file()
            ),
            None,
        )
    values = dict(os.environ)
    if candidate:
        values.update({k: v for k, v in dotenv_values(candidate).items() if v is not None})
    return values, candidate
