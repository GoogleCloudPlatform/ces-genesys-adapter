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

"""Tests for FORCE_ADAPTER_SESSION_ID session-ID selection."""

import dataclasses
import json
import unittest
from unittest import mock

from src import ces_ws
from src import genesys_ws
from src import settings as settings_lib

_AGENT_ID = "projects/test-proj/locations/us/apps/app-1"


def _settings(force: bool) -> settings_lib.Settings:
    return dataclasses.replace(
        settings_lib.settings, force_adapter_session_id=force
    )


def _open_message(input_variables: dict[str, str]) -> str:
    return json.dumps(
        {
            "type": "open",
            "seq": 1,
            "id": "client-id",
            "parameters": {
                "conversationId": "genesys-conv-999",
                "inputVariables": input_variables,
                "media": [{"type": "audio", "format": "PCMU", "rate": 8000}],
            },
        }
    )


class CesWsForceAdapterSessionIdTest(unittest.IsolatedAsyncioTestCase):

    async def _connect(
        self, force: bool, adapter_session_id: str, session_id: str | None
    ) -> str:
        ces_client = ces_ws.CESWS(
            mock.MagicMock(), adapter_session_id=adapter_session_id
        )
        provider = ces_ws.auth_provider
        with (
            mock.patch.object(ces_ws, "settings", _settings(force)),
            mock.patch.object(
                provider,
                "get_project_id",
                new_callable=mock.AsyncMock,
                return_value="test-proj",
            ),
            mock.patch.object(
                provider,
                "get_token",
                new_callable=mock.AsyncMock,
                return_value="test-token",
            ),
            mock.patch.object(
                ces_ws.websockets, "connect", new_callable=mock.AsyncMock
            ) as mock_connect,
        ):
            mock_connect.return_value = mock.AsyncMock()
            await ces_client.connect(agent_id=_AGENT_ID, session_id=session_id)
        return ces_client.session_id

    async def test_default_behavior_uses_passed_session_id(self):
        session_name = await self._connect(
            False, "adapter-uuid-1", "genesys-conv-id-123"
        )

        self.assertEqual(
            session_name, f"{_AGENT_ID}/sessions/genesys-conv-id-123"
        )

    async def test_default_behavior_falls_back_to_adapter_session_id(self):
        session_name = await self._connect(False, "adapter-uuid-1", None)

        self.assertEqual(session_name, f"{_AGENT_ID}/sessions/adapter-uuid-1")

    async def test_force_adapter_session_id_overrides_passed_session_id(self):
        session_name = await self._connect(
            True, "adapter-uuid-forced", "genesys-conv-id-123"
        )

        self.assertEqual(
            session_name, f"{_AGENT_ID}/sessions/adapter-uuid-forced"
        )


class GenesysWsForceAdapterSessionIdTest(unittest.IsolatedAsyncioTestCase):

    def _make_genesys_ws(self, adapter_session_id: str) -> genesys_ws.GenesysWS:
        handler = genesys_ws.GenesysWS(
            mock.AsyncMock(), adapter_session_id=adapter_session_id
        )
        handler.send_message = mock.AsyncMock()
        handler.ces_ws = mock.AsyncMock()
        handler.ces_ws.connect = mock.AsyncMock(return_value=True)
        return handler

    async def test_default_session_id_prefers_conversation_id(self):
        handler = self._make_genesys_ws("adapter-uuid-2")
        message = _open_message({"_agent_id": _AGENT_ID})

        with mock.patch.object(genesys_ws, "settings", _settings(False)):
            await handler.handle_text_message(message)

        self.assertEqual(handler.session_id, "genesys-conv-999")
        self.assertEqual(handler.conversation_id, "genesys-conv-999")

    async def test_force_adapter_session_id_overrides_in_genesys_ws(self):
        adapter_id = "adapter-uuid-forced-2"
        handler = self._make_genesys_ws(adapter_id)
        message = _open_message(
            {"_agent_id": _AGENT_ID, "_session_id": "explicit-session-id"}
        )

        with mock.patch.object(genesys_ws, "settings", _settings(True)):
            await handler.handle_text_message(message)

        self.assertEqual(handler.session_id, adapter_id)
        # The Genesys conversationId is kept for external correlation.
        self.assertEqual(handler.conversation_id, "genesys-conv-999")


if __name__ == "__main__":
    unittest.main()
