# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tests for settings parsing and ADC project-ID caching."""

import dataclasses
import unittest
from unittest import mock

from src import auth
from src import settings as settings_lib
from src.backends import base
from src.backends import ces_websocket

_AGENT_ID = "projects/test-proj/locations/us/apps/app-1"


class LoadSettingsTest(unittest.TestCase):

    def test_defaults_when_environment_is_empty(self):
        loaded = settings_lib.load_settings({}, resolve_secrets=False)

        self.assertEqual(loaded, settings_lib.Settings())
        self.assertEqual(loaded.port, 8080)
        self.assertEqual(
            loaded.disconnect_event_name, "sys.remote-call-disconnected"
        )
        self.assertFalse(loaded.force_adapter_session_id)
        self.assertFalse(loaded.log_unredacted_data)
        self.assertIsNone(loaded.auth_token_secret_path)

    def test_parses_all_values(self):
        env = {
            "PORT": "9090",
            "GENESYS_API_KEY": "key",
            "GENESYS_CLIENT_SECRET": "c2VjcmV0",
            "AUTH_TOKEN_SECRET_PATH": "projects/p/secrets/s",
            "LOG_UNREDACTED_DATA": "true",
            "LOG_LEVEL": "DEBUG",
            "DEBUG_WEBSOCKETS": "true",
            "DISCONNECT_EVENT_NAME": "custom.disconnect",
            "FORCE_ADAPTER_SESSION_ID": "TRUE",
            "HEALTH_WINDOW_SECONDS": "30",
            "HEALTH_MIN_SAMPLES": "5",
            "MAX_CES_ERROR_RATE_PCT": "10.5",
            "MAX_CES_AVG_LATENCY_MS": "900",
            "MAX_EVENT_LOOP_LAG_MS": "250",
        }

        loaded = settings_lib.load_settings(env, resolve_secrets=False)

        self.assertEqual(loaded.port, 9090)
        self.assertEqual(loaded.genesys_api_key, "key")
        self.assertEqual(loaded.genesys_client_secret, "c2VjcmV0")
        self.assertEqual(loaded.auth_token_secret_path, "projects/p/secrets/s")
        self.assertTrue(loaded.log_unredacted_data)
        self.assertEqual(loaded.log_level, "DEBUG")
        self.assertTrue(loaded.debug_websockets)
        self.assertEqual(loaded.disconnect_event_name, "custom.disconnect")
        self.assertTrue(loaded.force_adapter_session_id)
        self.assertEqual(loaded.health_window_seconds, 30)
        self.assertEqual(loaded.health_min_samples, 5)
        self.assertEqual(loaded.max_ces_error_rate_pct, 10.5)
        self.assertEqual(loaded.max_ces_avg_latency_ms, 900.0)
        self.assertEqual(loaded.max_event_loop_lag_ms, 250.0)

    def test_boolean_semantics_match_previous_releases(self):
        env = {
            "LOG_UNREDACTED_DATA": "True",
            "DEBUG_WEBSOCKETS": "TRUE",
            "FORCE_ADAPTER_SESSION_ID": "True",
        }

        loaded = settings_lib.load_settings(env, resolve_secrets=False)

        # These two have always been exact-match "true".
        self.assertFalse(loaded.log_unredacted_data)
        self.assertFalse(loaded.debug_websockets)
        # FORCE_ADAPTER_SESSION_ID has always been case-insensitive.
        self.assertTrue(loaded.force_adapter_session_id)

    def test_empty_numeric_values_fall_back_to_defaults(self):
        env = {"PORT": "", "HEALTH_MIN_SAMPLES": ""}

        loaded = settings_lib.load_settings(env, resolve_secrets=False)

        self.assertEqual(loaded.port, 8080)
        self.assertEqual(loaded.health_min_samples, 15)

    def test_invalid_integer_raises_settings_error(self):
        with self.assertRaisesRegex(
            settings_lib.SettingsError, "HEALTH_MIN_SAMPLES must be an integer"
        ):
            settings_lib.load_settings(
                {"HEALTH_MIN_SAMPLES": "lots"}, resolve_secrets=False
            )

    def test_invalid_float_raises_settings_error(self):
        with self.assertRaisesRegex(
            settings_lib.SettingsError,
            "MAX_CES_AVG_LATENCY_MS must be a number",
        ):
            settings_lib.load_settings(
                {"MAX_CES_AVG_LATENCY_MS": "fast"}, resolve_secrets=False
            )

    def test_settings_are_immutable(self):
        loaded = settings_lib.load_settings({}, resolve_secrets=False)

        with self.assertRaisesRegex(
            dataclasses.FrozenInstanceError, "cannot assign to field"
        ):
            loaded.port = 1

    def test_secret_paths_are_resolved_when_enabled(self):
        secret_path = "projects/p/secrets/k"
        with mock.patch.object(
            settings_lib, "resolve_secret", side_effect=lambda v: v and f"r:{v}"
        ) as mock_resolve:
            loaded = settings_lib.load_settings(
                {"GENESYS_API_KEY": secret_path}
            )

        self.assertEqual(loaded.genesys_api_key, f"r:{secret_path}")
        mock_resolve.assert_any_call(secret_path)


class AuthProjectIdTest(unittest.IsolatedAsyncioTestCase):

    async def test_initialize_adc_caches_project_id(self):
        auth_provider = auth.Auth()
        with mock.patch.object(
            auth.google.auth,
            "default",
            return_value=(mock.MagicMock(), "test-proj"),
        ) as mock_default:
            self.assertEqual(auth_provider.initialize_adc(), "test-proj")
            self.assertEqual(await auth_provider.get_project_id(), "test-proj")
            self.assertEqual(await auth_provider.get_project_id(), "test-proj")

        mock_default.assert_called_once()

    async def test_get_project_id_resolves_lazily_once(self):
        auth_provider = auth.Auth()
        with mock.patch.object(
            auth.google.auth,
            "default",
            return_value=(mock.MagicMock(), "lazy-proj"),
        ) as mock_default:
            self.assertEqual(await auth_provider.get_project_id(), "lazy-proj")
            self.assertEqual(await auth_provider.get_project_id(), "lazy-proj")

        mock_default.assert_called_once()

    async def test_ces_connect_uses_cached_project_id(self):
        backend = ces_websocket.CesWebSocketBackend(lambda *_: {})
        start = base.SessionStart(
            agent_id=_AGENT_ID,
            deployment_id=None,
            session_id="uuid",
            variables={},
        )
        provider = auth.auth_provider
        with (
            mock.patch.object(
                provider,
                "get_project_id",
                new_callable=mock.AsyncMock,
                return_value="cached-proj",
            ),
            mock.patch.object(
                provider,
                "get_token",
                new_callable=mock.AsyncMock,
                return_value="tok",
            ),
            mock.patch.object(auth.google.auth, "default") as mock_default,
            mock.patch.object(
                ces_websocket.websockets,
                "connect",
                new_callable=mock.AsyncMock,
            ) as mock_connect,
        ):
            mock_connect.return_value = mock.AsyncMock()
            await backend.open(start)

        mock_default.assert_not_called()
        headers = mock_connect.call_args.kwargs["additional_headers"]
        self.assertEqual(headers["X-Goog-User-Project"], "cached-proj")


if __name__ == "__main__":
    unittest.main()
