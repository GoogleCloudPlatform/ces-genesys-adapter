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

"""Unit tests for AudioHook media selection and server messages."""

import json
import unittest
from unittest import mock

from websockets import protocol

from src import genesys_ws

_MONO = {"type": "audio", "format": "PCMU", "channels": ["external"]}


def _expected(channel: str) -> dict:
    return {
        "type": "audio",
        "format": "PCMU",
        "channels": [channel],
        "rate": 8000,
    }


class SelectMediaTest(unittest.TestCase):

    def test_mono_with_rate(self):
        offer = [{**_MONO, "rate": 8000}]

        self.assertEqual(genesys_ws.select_media(offer), _expected("external"))

    def test_missing_rate_defaults_to_8000(self):
        self.assertEqual(
            genesys_ws.select_media([_MONO]), _expected("external")
        )

    def test_stereo_first_accepts_external_only(self):
        offer = [
            {**_MONO, "channels": ["external", "internal"], "rate": 8000},
            {**_MONO, "rate": 8000},
        ]

        self.assertEqual(genesys_ws.select_media(offer), _expected("external"))

    def test_internal_only_keeps_its_channel(self):
        offer = [{**_MONO, "channels": ["internal"]}]

        self.assertEqual(genesys_ws.select_media(offer), _expected("internal"))

    def test_missing_channels_default_to_external(self):
        offer = [{"type": "audio", "format": "PCMU", "rate": 8000}]

        self.assertEqual(genesys_ws.select_media(offer), _expected("external"))

    def test_skips_unsupported_offers(self):
        offer = [
            {**_MONO, "format": "L16"},
            {**_MONO, "rate": 16000},
            {**_MONO, "channels": ["internal"], "rate": 8000},
        ]

        self.assertEqual(genesys_ws.select_media(offer), _expected("internal"))

    def test_nothing_usable(self):
        self.assertIsNone(genesys_ws.select_media([{**_MONO, "format": "L16"}]))


class ServerMessagesTest(unittest.IsolatedAsyncioTestCase):

    def _handler(self) -> tuple[genesys_ws.GenesysWS, mock.AsyncMock]:
        socket = mock.AsyncMock()
        socket.state = protocol.State.OPEN
        handler = genesys_ws.GenesysWS(socket, "adapter-uuid-1")
        handler.client_session_id = "genesys-session-1"
        handler.last_client_sequence_number = 7
        return handler, socket

    async def test_barge_in_event(self):
        handler, socket = self._handler()

        await handler.send_barge_in()

        sent = json.loads(socket.send.call_args.args[0])
        self.assertEqual(
            sent,
            {
                "type": "event",
                "version": "2",
                "id": "genesys-session-1",
                "clientseq": 7,
                "seq": 1,
                "parameters": {"entities": [{"type": "barge_in", "data": {}}]},
            },
        )

    async def test_disconnect_defaults_to_completed(self):
        handler, socket = self._handler()

        await handler.send_disconnect(output_variables={"count": 2})

        sent = json.loads(socket.send.call_args.args[0])
        self.assertEqual(
            sent["parameters"],
            {"reason": "completed", "outputVariables": {"count": "2"}},
        )


if __name__ == "__main__":
    unittest.main()
