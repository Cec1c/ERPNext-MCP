from __future__ import annotations

from contextlib import asynccontextmanager
from contextvars import ContextVar

from .client import ERPNextClient
from .config import Settings

_settings: ContextVar[Settings | None] = ContextVar("erpnext_settings", default=None)
_client: ContextVar[ERPNextClient | None] = ContextVar("erpnext_client", default=None)


def get_settings() -> Settings:
    return _settings.get() or Settings.from_env()


def get_client() -> ERPNextClient:
    client = _client.get()
    if client is None:
        raise RuntimeError("ERPNext operations require an invocation context")
    return client


@asynccontextmanager
async def invocation(settings: Settings | None = None, client: ERPNextClient | None = None):
    """Pin one immutable configuration and connection for the entire tool invocation."""
    selected = settings or Settings.from_env()
    connection = client or ERPNextClient(selected)
    settings_token, client_token = _settings.set(selected), _client.set(connection)
    try:
        yield selected, connection
    finally:
        _client.reset(client_token)
        _settings.reset(settings_token)
        if client is None:
            await connection.aclose()
