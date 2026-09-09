"""Environment-backed configuration.

Values come from `.env`, which is gitignored. Nothing in this module ever logs
or prints a secret value.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

from dotenv import load_dotenv

load_dotenv()

BrokerMode = Literal["null", "paper", "ibkr"]


class ConfigError(RuntimeError):
    """Raised when required configuration is missing or invalid."""


def _require(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ConfigError(f"{name} is not set; copy .env.example to .env and fill it in")
    return value


@dataclass(frozen=True)
class SupabaseConfig:
    url: str
    service_role_key: str

    @classmethod
    def from_env(cls) -> SupabaseConfig:
        return cls(
            url=_require("SUPABASE_URL"),
            service_role_key=_require("SUPABASE_SERVICE_ROLE_KEY"),
        )


@dataclass(frozen=True)
class IBKRConfig:
    host: str
    port: int
    client_id: int

    @property
    def is_paper(self) -> bool:
        """True for the TWS paper-trading port.

        Port 7496 (TWS live) and 4001 (IB Gateway) are NOT assumed to be paper.
        """
        return self.port == 7497

    @classmethod
    def from_env(cls) -> IBKRConfig:
        return cls(
            host=os.environ.get("IBKR_HOST", "127.0.0.1"),
            port=int(os.environ.get("IBKR_PORT", "7497")),
            client_id=int(os.environ.get("IBKR_CLIENT_ID", "11")),
        )


def broker_mode() -> BrokerMode:
    """Execution mode. Defaults to 'null' (read-only) if unset or unrecognised."""
    mode = os.environ.get("BROKER_MODE", "null").strip().lower()
    if mode not in ("null", "paper", "ibkr"):
        raise ConfigError(f"BROKER_MODE must be null|paper|ibkr, got {mode!r}")
    return mode  # type: ignore[return-value]
