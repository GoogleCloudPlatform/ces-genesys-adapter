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

"""Unit tests for CES WebSocket message translation and close mapping."""

import base64
import json
import unittest
from unittest import mock

import websockets

from src.backends import base
from src.backends import ces_websocket


def _backend_receiving(message) -> ces_websocket.CesWebSocketBackend:
    """Returns a backend whose socket yields one message or raises it."""
    backend = ces_websocket.CesWebSocketBackend(lambda *_: {})
    socket = mock.AsyncMock()
    if isinstance(message, BaseException):
        socket.recv.side_effect = message
    else:
        socket.recv.return_value = json.dumps(message)
    backend._websocket = socket  # pylint: disable=protected-access
    return backend


class TranslateTest(unittest.IsolatedAsyncioTestCase):

    async def _receive(self, message) -> base.BackendEvent | None:
        return await _backend_receiving(message).receive()

    async def test_audio(self):
        audio = base64.b64encode(b"\x01\x02").decode()

        event = await self._receive({"sessionOutput": {"audio": audio}})

        self.assertEqual(event, base.AudioOut(b"\x01\x02"))

    async def test_interruption_takes_precedence(self):
        event = await self._receive(
            {"interruptionSignal": {}, "sessionOutput": {"text": "hi"}}
        )

        self.assertEqual(event, base.BargeIn())

    async def test_text(self):
        event = await self._receive({"sessionOutput": {"text": "hello"}})

        self.assertEqual(event, base.AgentText("hello"))

    async def test_end_session_params(self):
        event = await self._receive(
            {"endSession": {"metadata": {"params": {"a": "b"}}}}
        )

        self.assertEqual(event, base.EndOfInteraction({"a": "b"}))

    async def test_end_session_without_params(self):
        event = await self._receive({"endSession": {}})

        self.assertEqual(event, base.EndOfInteraction({}))

    async def test_go_away(self):
        event = await self._receive({"goAway": {}})

        self.assertEqual(event, base.Rollover("goAway"))

    async def test_recognition_result_needs_no_action(self):
        self.assertIsNone(await self._receive({"recognitionResult": {}}))

    async def test_unknown_message_needs_no_action(self):
        self.assertIsNone(await self._receive({"somethingNew": {}}))

    async def test_invalid_json_is_a_backend_error(self):
        backend = _backend_receiving({})
        backend._websocket.recv.return_value = "not json"  # pylint: disable=protected-access

        event = await backend.receive()

        self.assertIsInstance(event, base.BackendError)
        self.assertTrue(event.message.startswith("CES Listen Error"))

    async def test_close_frame_maps_to_stream_ended(self):
        close_frame = websockets.frames.Close(1011, "RESOURCE_EXHAUSTED")
        error = websockets.exceptions.ConnectionClosedError(close_frame, None)

        event = await self._receive(error)

        self.assertEqual(event.code, 1011)
        self.assertFalse(event.clean)
        self.assertEqual(event.sys_error, "CES_RESOURCE_EXHAUSTED")

    async def test_missing_close_frame_is_abnormal(self):
        error = websockets.exceptions.ConnectionClosedError(None, None)

        event = await self._receive(error)

        self.assertEqual(event.code, 1006)
        self.assertIsNone(event.sys_error)


class CloseSysErrorTest(unittest.TestCase):

    def test_mapping(self):
        cases = [
            (1011, "resource_exhausted", "CES_RESOURCE_EXHAUSTED"),
            (1011, "internal", None),
            (1007, "Timed out waiting for 30s", "CES_UNSUPPORTED_PAYLOAD"),
            (1008, "aborted", None),
        ]
        for code, reason, expected in cases:
            with self.subTest(code=code, reason=reason):
                self.assertEqual(
                    ces_websocket.close_sys_error(code, reason), expected
                )


class LocationFromAgentIdTest(unittest.TestCase):

    def test_location(self):
        self.assertEqual(
            ces_websocket.location_from_agent_id(
                "projects/p/locations/eu/apps/a"
            ),
            "eu",
        )

    def test_missing_location(self):
        self.assertIsNone(ces_websocket.location_from_agent_id("projects/p"))


if __name__ == "__main__":
    unittest.main()
