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

"""Tests for the FastAPI adapter, Genesys close handling and auth logging."""

import dataclasses
import unittest
from unittest import mock

import websockets

from src import auth
from src import genesys_ws
from src import main
from src import settings as settings_lib


def _disconnect_message(code: int, reason: str) -> dict[str, object]:
    return {"type": "websocket.disconnect", "code": code, "reason": reason}


class FastApiWebSocketAdapterTest(unittest.IsolatedAsyncioTestCase):

    async def _recv_with_disconnect(self, code: int, reason: str):
        fastapi_ws = mock.MagicMock()
        fastapi_ws.receive = mock.AsyncMock(
            return_value=_disconnect_message(code, reason)
        )
        adapter = main.FastApiWebSocketAdapter(fastapi_ws)
        await adapter.recv()

    async def test_recv_disconnect_code_1000_raises_connection_closed_ok(self):
        with self.assertRaisesRegex(
            websockets.exceptions.ConnectionClosedOK, "1000"
        ) as ctx:
            await self._recv_with_disconnect(1000, "Normal Closure")

        self.assertEqual(ctx.exception.code, 1000)

    async def test_recv_disconnect_code_1001_raises_connection_closed_ok(self):
        with self.assertRaisesRegex(
            websockets.exceptions.ConnectionClosedOK, "1001"
        ) as ctx:
            await self._recv_with_disconnect(1001, "Going Away")

        self.assertEqual(ctx.exception.code, 1001)

    async def test_recv_disconnect_code_1006_raises_connection_closed_error(
        self,
    ):
        with self.assertRaisesRegex(
            websockets.exceptions.ConnectionClosedError, "1006"
        ) as ctx:
            await self._recv_with_disconnect(1006, "Abnormal Closure")

        self.assertEqual(ctx.exception.code, 1006)


class GenesysWsConnectionClosedTest(unittest.IsolatedAsyncioTestCase):

    async def test_connection_closed_ok_is_logged_as_info(self):
        close_frame = websockets.frames.Close(1000, "Normal Closure")
        closed_ok = websockets.exceptions.ConnectionClosedOK(close_frame, None)
        genesys_socket = mock.MagicMock()
        genesys_socket.__aiter__ = mock.MagicMock(return_value=genesys_socket)
        genesys_socket.__anext__ = mock.AsyncMock(side_effect=closed_ok)
        handler = genesys_ws.GenesysWS(
            genesys_socket, adapter_session_id="test-session-123"
        )

        with mock.patch.object(genesys_ws, "logger") as mock_logger:
            await handler.handle_connection()

        error_calls = [
            call
            for call in mock_logger.error.call_args_list
            if "closed unexpectedly" in str(call)
            or "genesys_error" in str(call)
        ]
        self.assertFalse(error_calls)
        info_calls = [
            call
            for call in mock_logger.info.call_args_list
            if "Genesys WebSocket closed as expected" in str(call)
        ]
        self.assertTrue(info_calls)


class SecretLoggingTest(unittest.TestCase):

    def test_verify_request_logs_lengths_only(self):
        test_settings = dataclasses.replace(
            settings_lib.settings,
            genesys_api_key="expected-api-key-987",
            genesys_client_secret="client-secret-12345",
        )
        request = mock.MagicMock()
        request.headers = {"x-api-key": "received-api-key"}
        request.path = "/api/v1/audiohook"

        with (
            mock.patch.object(auth, "settings", test_settings),
            mock.patch.object(auth, "logger") as mock_logger,
        ):
            result = auth.auth_provider.verify_request(request)

        self.assertFalse(result)
        info_logs = [str(call) for call in mock_logger.info.call_args_list]
        self.assertTrue(
            any(
                "received_length=16, expected_length=20" in log
                for log in info_logs
            )
        )
        self.assertFalse(any("received-api-key" in log for log in info_logs))
        self.assertFalse(
            any("expected-api-key-987" in log for log in info_logs)
        )


if __name__ == "__main__":
    unittest.main()
