"""Shared environment / cluster configuration loading.

For now this is just environment variables. Later this can grow a config-file
layer (e.g. ~/.config/veritaslock/config.toml) and per-cluster contexts.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_ENV = "local"
DEFAULT_API_BASE_URL = "http://localhost:8080"
DEFAULT_KAFKA_BOOTSTRAP = "localhost:9092"


@dataclass(frozen=True)
class Config:
    """Resolved configuration for a `vl` invocation."""

    env: str
    api_base_url: str
    kafka_bootstrap: str


def load_config() -> Config:
    """Load configuration from the environment.

    Environment variables:
      VL_ENV             — target environment name (default: "local")
      VL_API_BASE_URL    — IdP / Control-Plane API base URL
      VL_KAFKA_BOOTSTRAP — Kafka bootstrap servers for `vl events`
    """
    return Config(
        env=os.environ.get("VL_ENV", DEFAULT_ENV),
        api_base_url=os.environ.get("VL_API_BASE_URL", DEFAULT_API_BASE_URL),
        kafka_bootstrap=os.environ.get("VL_KAFKA_BOOTSTRAP", DEFAULT_KAFKA_BOOTSTRAP),
    )
