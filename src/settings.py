# Copyright 2025 Google LLC

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     https://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Typed, validated runtime settings loaded once from the environment."""

from collections.abc import Callable, Mapping
import dataclasses
import os

import dotenv
from google.cloud import secretmanager

dotenv.load_dotenv()

_DEFAULT_DISCONNECT_EVENT_NAME = "sys.remote-call-disconnected"


class SettingsError(ValueError):
    """Raised when an environment variable has an invalid value."""


def resolve_secret(secret_value: str | None) -> str | None:
    """Returns a secret value, fetching it if it is a Secret Manager path.

    Args:
        secret_value: A literal value, a Secret Manager resource path
            (`projects/...`), or None.

    Returns:
        The literal value, the fetched secret payload, or None if
        `secret_value` is None or empty.

    Raises:
        google.api_core.exceptions.GoogleAPIError: If the secret cannot be
            fetched.
    """
    if not secret_value or not secret_value.startswith("projects/"):
        return secret_value
    secret_path = secret_value
    if "/versions/" not in secret_path:
        secret_path = f"{secret_path}/versions/latest"
    try:
        client = secretmanager.SecretManagerServiceClient()
        response = client.access_secret_version(name=secret_path)
    except Exception as e:
        print(f"Error fetching secret from Secret Manager: {e}")
        raise
    return response.payload.data.decode("UTF-8").strip()


@dataclasses.dataclass(frozen=True)
class Settings:
    """Adapter settings.

    Instances are immutable. Tests should derive variants with
    `dataclasses.replace()`.
    """

    port: int = 8080
    genesys_api_key: str | None = None
    genesys_client_secret: str | None = None
    auth_token_secret_path: str | None = None
    log_unredacted_data: bool = False
    log_level: str = "INFO"
    debug_websockets: bool = False
    disconnect_event_name: str = _DEFAULT_DISCONNECT_EVENT_NAME
    force_adapter_session_id: bool = False
    health_window_seconds: int = 60
    health_min_samples: int = 15
    max_ces_error_rate_pct: float = 25.0
    max_ces_avg_latency_ms: float = 1500.0
    max_event_loop_lag_ms: float = 500.0


def _parse_int(env: Mapping[str, str], name: str, default: int) -> int:
    """Parses an integer env var, returning `default` when unset or empty."""
    raw_value = env.get(name)
    if not raw_value:
        return default
    try:
        return int(raw_value)
    except ValueError as e:
        raise SettingsError(
            f"{name} must be an integer, got {raw_value!r}"
        ) from e


def _parse_float(env: Mapping[str, str], name: str, default: float) -> float:
    """Parses a float env var, returning `default` when unset or empty."""
    raw_value = env.get(name)
    if not raw_value:
        return default
    try:
        return float(raw_value)
    except ValueError as e:
        raise SettingsError(
            f"{name} must be a number, got {raw_value!r}"
        ) from e


def _identity(value: str | None) -> str | None:
    return value


def load_settings(
    env: Mapping[str, str] | None = None,
    *,
    resolve_secrets: bool = True,
) -> Settings:
    """Builds Settings from environment variables.

    Args:
        env: Mapping to read from. Defaults to `os.environ` when None.
        resolve_secrets: Whether to fetch Secret Manager paths given for the
            Genesys credentials.

    Returns:
        The parsed, immutable settings.

    Raises:
        SettingsError: If a value cannot be parsed.
    """
    env = os.environ if env is None else env
    resolve: Callable[[str | None], str | None] = (
        resolve_secret if resolve_secrets else _identity
    )
    force_adapter_session_id = env.get("FORCE_ADAPTER_SESSION_ID", "false")
    return Settings(
        port=_parse_int(env, "PORT", 8080),
        genesys_api_key=resolve(env.get("GENESYS_API_KEY")),
        genesys_client_secret=resolve(env.get("GENESYS_CLIENT_SECRET")),
        auth_token_secret_path=env.get("AUTH_TOKEN_SECRET_PATH") or None,
        # Exact-match "true" semantics are preserved from earlier releases.
        log_unredacted_data=env.get("LOG_UNREDACTED_DATA") == "true",
        log_level=env.get("LOG_LEVEL", "INFO"),
        debug_websockets=env.get("DEBUG_WEBSOCKETS", "false") == "true",
        disconnect_event_name=env.get(
            "DISCONNECT_EVENT_NAME", _DEFAULT_DISCONNECT_EVENT_NAME
        ),
        force_adapter_session_id=force_adapter_session_id.lower() == "true",
        health_window_seconds=_parse_int(env, "HEALTH_WINDOW_SECONDS", 60),
        health_min_samples=_parse_int(env, "HEALTH_MIN_SAMPLES", 15),
        max_ces_error_rate_pct=_parse_float(
            env, "MAX_CES_ERROR_RATE_PCT", 25.0
        ),
        max_ces_avg_latency_ms=_parse_float(
            env, "MAX_CES_AVG_LATENCY_MS", 1500.0
        ),
        max_event_loop_lag_ms=_parse_float(env, "MAX_EVENT_LOOP_LAG_MS", 500.0),
    )


settings = load_settings()
