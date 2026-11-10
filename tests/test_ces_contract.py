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

"""Contract scenarios for a full call in `ces` mode.

Each scenario drives `GenesysWS` with a fake Genesys socket against a fake
CES BidiRunSession server on localhost, so the whole path (AudioHook
protocol, call session, pacer and CES WebSocket backend) is exercised
without network access or credentials. The same scenarios will run against
the Dialogflow backend once it exists.
"""

import asyncio
import base64
from collections.abc import Awaitable, Callable
import contextlib
import json
import os
import socket
from typing import Any
import unittest
from unittest import mock

import websockets
from websockets import protocol
from websockets.asyncio import server as ws_server

from src import auth
from src import call_session
from src import genesys_ws
from src.backends import ces_websocket

_AGENT_ID = "projects/test-proj/locations/us/apps/app-1"
_DEPLOYMENT_ID = f"{_AGENT_ID}/deployments/dep-1"
_CONVERSATION_ID = "11111111-2222-3333-4444-555555555555"
_DISCONNECT_EVENT = "sys.remote-call-disconnected"
_TIMEOUT_S = 5.0

# A CES script receives the connection index and the connection, and plays
# the server side of the scenario.
CesScript = Callable[[int, "FakeCesConnection"], Awaitable[None]]


class FakeGenesysSocket:
    """In-memory stand-in for the Genesys AudioHook WebSocket."""

    def __init__(self):
        self.state = protocol.State.OPEN
        self.sent: list[str | bytes] = []
        self._inbound: asyncio.Queue[str | bytes | None] = asyncio.Queue()

    def __aiter__(self):
        return self

    async def __anext__(self) -> str | bytes:
        message = await self._inbound.get()
        if message is None:
            raise StopAsyncIteration
        return message

    async def send(self, data: str | bytes) -> None:
        if self.state != protocol.State.OPEN:
            raise websockets.exceptions.ConnectionClosedOK(None, None)
        self.sent.append(data)

    def push(self, message: dict[str, Any] | bytes | None) -> None:
        """Queues a message from Genesys; None ends the connection."""
        if isinstance(message, dict):
            message = json.dumps(message)
        self._inbound.put_nowait(message)

    def end(self) -> None:
        """Closes the socket the way Genesys does after `disconnect`."""
        self.state = protocol.State.CLOSED
        self.push(None)

    def messages(self, message_type: str | None = None) -> list[dict]:
        """Returns the JSON messages sent to Genesys, optionally filtered."""
        parsed = [json.loads(m) for m in self.sent if isinstance(m, str)]
        if message_type is None:
            return parsed
        return [m for m in parsed if m.get("type") == message_type]

    def audio_bytes(self) -> int:
        """Returns the number of audio bytes sent to Genesys."""
        return sum(len(m) for m in self.sent if isinstance(m, bytes))


class FakeCesConnection:
    """Server side of one CES WebSocket connection."""

    def __init__(self, connection: ws_server.ServerConnection):
        self.connection = connection
        self.received: list[dict[str, Any]] = []
        self.closed = asyncio.Event()

    async def send(self, message: dict[str, Any]) -> None:
        await self.connection.send(json.dumps(message))

    async def send_audio(self, size: int) -> None:
        audio = base64.b64encode(b"\x7f" * size).decode()
        await self.send({"sessionOutput": {"audio": audio}})

    async def wait_for(self, predicate: Callable[[dict], bool]) -> dict:
        """Waits until a received message matches the predicate."""
        async with asyncio.timeout(_TIMEOUT_S):
            while True:
                for message in self.received:
                    if predicate(message):
                        return message
                await asyncio.sleep(0.01)

    async def read_forever(self) -> None:
        try:
            async for raw in self.connection:
                self.received.append(json.loads(raw))
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self.closed.set()


class FakeCesServer:
    """A localhost WebSocket server that plays a CES script."""

    def __init__(self, script: CesScript, reject_first: int = 0):
        self._script = script
        self._reject_remaining = reject_first
        self.rejected = 0
        self.connections: list[FakeCesConnection] = []
        self._server = None

    @property
    def base_url(self) -> str:
        port = self._server.sockets[0].getsockname()[1]
        return f"ws://127.0.0.1:{port}/ws/BidiRunSession/locations/"

    async def __aenter__(self) -> "FakeCesServer":
        self._server = await ws_server.serve(
            self._handle, "127.0.0.1", 0, process_request=self._process_request
        )
        return self

    async def __aexit__(self, *exc_info) -> None:
        self._server.close()
        await self._server.wait_closed()

    def _process_request(self, connection, request):
        del request  # Unused.
        if self._reject_remaining <= 0:
            return None
        self._reject_remaining -= 1
        self.rejected += 1
        return connection.respond(503, "Service Unavailable\n")

    async def _handle(self, connection: ws_server.ServerConnection) -> None:
        ces = FakeCesConnection(connection)
        index = len(self.connections)
        self.connections.append(ces)
        reader = asyncio.create_task(ces.read_forever())
        await self._script(index, ces)
        await reader


def _open_message(input_variables: dict[str, str], conv_id: str) -> dict:
    return {
        "version": "2",
        "type": "open",
        "seq": 1,
        "id": "genesys-session-1",
        "parameters": {
            "conversationId": conv_id,
            "inputVariables": input_variables,
            "media": [{"type": "audio", "format": "PCMU", "rate": 8000}],
        },
    }


def _is_event(name: str) -> Callable[[dict], bool]:
    return (
        lambda m: m.get("realtimeInput", {}).get("event", {}).get("event")
        == name
    )


def _unused_port_url() -> str:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"ws://127.0.0.1:{port}/ws/BidiRunSession/locations/"


async def _wait_until(condition: Callable[[], bool]) -> None:
    async with asyncio.timeout(_TIMEOUT_S):
        while not condition():
            await asyncio.sleep(0.01)


class CesContractTest(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self._stack = contextlib.AsyncExitStack()
        # websockets honors proxy env vars; keep loopback traffic direct.
        self._stack.enter_context(
            mock.patch.dict(
                os.environ,
                {"no_proxy": "127.0.0.1,localhost", "NO_PROXY": "127.0.0.1"},
            )
        )
        provider = auth.auth_provider
        for name, value in (
            ("get_project_id", "test-proj"),
            ("get_token", "t"),
        ):
            self._stack.enter_context(
                mock.patch.object(
                    provider,
                    name,
                    new_callable=mock.AsyncMock,
                    return_value=value,
                )
            )
        self.genesys = FakeGenesysSocket()
        self.handler = genesys_ws.GenesysWS(self.genesys, "adapter-uuid-1")

    async def asyncTearDown(self):
        self.genesys.end()
        await asyncio.wait_for(self._handler_task, _TIMEOUT_S)
        if self.handler._teardown_task:  # pylint: disable=protected-access
            await asyncio.wait_for(
                self.handler._teardown_task,  # pylint: disable=protected-access
                _TIMEOUT_S,
            )
        await self._stack.aclose()

    def _use_backend_url(self, base_url: str) -> None:
        self._stack.enter_context(
            mock.patch.object(
                call_session.factory,
                "create_backend",
                lambda log_extra: ces_websocket.CesWebSocketBackend(
                    log_extra, base_url=base_url
                ),
            )
        )

    async def _start_call(
        self,
        server: FakeCesServer | None,
        input_variables: dict[str, str] | None = None,
        conv_id: str = _CONVERSATION_ID,
    ) -> None:
        self._use_backend_url(server.base_url if server else _unused_port_url())
        self._handler_task = asyncio.create_task(
            self.handler.handle_connection()
        )
        self.genesys.push(
            _open_message(
                input_variables or {"_deployment_id": _DEPLOYMENT_ID}, conv_id
            )
        )

    async def _wait_for_disconnect(self) -> dict:
        await _wait_until(lambda: self.genesys.messages("disconnect"))
        return self.genesys.messages("disconnect")[0]

    async def test_greeting_audio_and_agent_end_with_outputs(self):
        async def script(_, ces: FakeCesConnection) -> None:
            await ces.wait_for(_is_event("session_start"))
            await ces.send_audio(1600)
            await ces.send(
                {
                    "endSession": {
                        "metadata": {
                            "params": {"disposition": "Resolved", "count": 2}
                        }
                    }
                }
            )
            await ces.connection.close()

        async with FakeCesServer(script) as server:
            await self._start_call(
                server, {"_deployment_id": _DEPLOYMENT_ID, "tier": "gold"}
            )
            disconnect = await self._wait_for_disconnect()

        ces = server.connections[0]
        self.assertEqual(
            ces.received[0]["config"]["session"],
            f"{_AGENT_ID}/sessions/{_CONVERSATION_ID}",
        )
        self.assertEqual(
            ces.received[0]["config"]["deployment"], _DEPLOYMENT_ID
        )
        self.assertEqual(
            ces.received[1],
            {
                "realtimeInput": {
                    "variables": {
                        "tier": "gold",
                        "adapterSessionId": "adapter-uuid-1",
                    }
                }
            },
        )
        self.assertTrue(_is_event("session_start")(ces.received[2]))
        self.assertEqual(len(self.genesys.messages("opened")), 1)
        self.assertEqual(self.genesys.audio_bytes(), 1600)
        self.assertEqual(
            disconnect["parameters"],
            {
                "reason": "completed",
                "info": "Session has ended successfully in CES",
                "outputVariables": {"disposition": "Resolved", "count": "2"},
            },
        )

    async def test_initial_message_is_sent_as_text(self):
        async def script(_, ces: FakeCesConnection) -> None:
            await ces.wait_for(
                lambda m: "realtimeInput" in m and "text" in m["realtimeInput"]
            )
            await ces.connection.close()

        async with FakeCesServer(script) as server:
            await self._start_call(
                server,
                {"_agent_id": _AGENT_ID, "_initial_message": "hello agent"},
            )
            await _wait_until(
                lambda: server.connections
                and server.connections[0].closed.is_set()
            )

        received = server.connections[0].received
        self.assertNotIn("deployment", received[0]["config"])
        self.assertEqual(
            received[-1], {"realtimeInput": {"text": "hello agent"}}
        )
        self.assertFalse(any(_is_event("session_start")(m) for m in received))

    async def test_barge_in_discards_queued_audio(self):
        async def script(_, ces: FakeCesConnection) -> None:
            await ces.wait_for(_is_event("session_start"))
            await ces.send_audio(24000)  # 3 s of agent audio.
            await ces.send({"interruptionSignal": {}})
            await ces.send({"endSession": {"metadata": {}}})
            await ces.connection.close()

        async with FakeCesServer(script) as server:
            await self._start_call(server)
            await self._wait_for_disconnect()

        # Only the 500 ms prime (and at most one paced send) went out, and
        # Genesys was told to discard what it had buffered (TC-5.1).
        self.assertLess(self.genesys.audio_bytes(), 12000)
        events = self.genesys.messages("event")
        self.assertEqual(
            events[0]["parameters"],
            {"entities": [{"type": "barge_in", "data": {}}]},
        )

    async def test_dtmf_is_forwarded(self):
        async def script(_, ces: FakeCesConnection) -> None:
            await ces.wait_for(lambda m: m.get("realtimeInput", {}).get("dtmf"))
            await ces.connection.close()

        async with FakeCesServer(script) as server:
            await self._start_call(server)
            await _wait_until(lambda: self.genesys.messages("opened"))
            self.genesys.push(
                {
                    "type": "dtmf",
                    "seq": 2,
                    "id": "genesys-session-1",
                    "parameters": {"digit": "5"},
                }
            )
            await asyncio.wait_for(
                server.connections[0].closed.wait(), _TIMEOUT_S
            )

        self.assertIn(
            {"realtimeInput": {"dtmf": "5"}}, server.connections[0].received
        )

    async def test_caller_hangup_signals_ces_and_closes(self):
        async def script(_, ces: FakeCesConnection) -> None:
            await ces.wait_for(_is_event(_DISCONNECT_EVENT))
            await ces.send({"endSession": {"metadata": {}}})
            await ces.closed.wait()

        async with FakeCesServer(script) as server:
            await self._start_call(server)
            await _wait_until(lambda: self.genesys.messages("opened"))
            self.genesys.push(
                {"type": "close", "seq": 2, "id": "genesys-session-1"}
            )
            await _wait_until(lambda: self.genesys.messages("closed"))
            await asyncio.wait_for(
                server.connections[0].closed.wait(), _TIMEOUT_S
            )

        received = server.connections[0].received
        self.assertTrue(any(_is_event(_DISCONNECT_EVENT)(m) for m in received))
        self.assertIn({"clientHalfClose": True}, received)
        self.assertFalse(self.genesys.messages("disconnect"))

    async def test_goaway_reconnects_to_same_session(self):
        async def script(index: int, ces: FakeCesConnection) -> None:
            await ces.wait_for(_is_event("session_start"))
            if index == 0:
                await ces.send({"goAway": {}})
                await ces.closed.wait()
                return
            await ces.send_audio(800)
            await ces.send({"endSession": {"metadata": {"params": {"k": "v"}}}})
            await ces.connection.close()

        async with FakeCesServer(script) as server:
            await self._start_call(server)
            disconnect = await self._wait_for_disconnect()

        self.assertEqual(len(server.connections), 2)
        first, second = (c.received[0]["config"] for c in server.connections)
        self.assertEqual(first["session"], second["session"])
        # Current behavior, to be changed by D10: the reconnect replays a
        # historicalContexts chunk and repeats the kickstart.
        self.assertIn("historicalContexts", second)
        self.assertEqual(disconnect["parameters"]["reason"], "completed")
        self.assertEqual(
            disconnect["parameters"]["outputVariables"], {"k": "v"}
        )

    async def test_unreachable_ces_disconnects_with_error(self):
        await self._start_call(None)

        disconnect = await self._wait_for_disconnect()

        self.assertEqual(disconnect["parameters"]["reason"], "error")
        self.assertTrue(
            disconnect["parameters"]["info"].startswith(
                "CES Connection/Config Error"
            )
        )
        self.assertFalse(self.genesys.messages("opened"))

    async def test_probe_never_opens_a_backend(self):
        async def script(_, ces: FakeCesConnection) -> None:
            await ces.closed.wait()

        async with FakeCesServer(script) as server:
            await self._start_call(
                server, conv_id=genesys_ws.PROBE_CONVERSATION_ID
            )
            await _wait_until(lambda: self.genesys.messages("opened"))

        self.assertFalse(server.connections)

    async def test_resource_exhausted_close_maps_to_sys_error(self):
        async def script(_, ces: FakeCesConnection) -> None:
            await ces.wait_for(_is_event("session_start"))
            await ces.connection.close(1011, "RESOURCE_EXHAUSTED: quota")

        async with FakeCesServer(script) as server:
            await self._start_call(server)
            disconnect = await self._wait_for_disconnect()

        self.assertEqual(
            disconnect["parameters"],
            {
                "reason": "completed",
                "outputVariables": {"SYS_ERROR": "CES_RESOURCE_EXHAUSTED"},
            },
        )

    async def test_end_session_disconnects_without_waiting_for_ces(self):
        async def script(_, ces: FakeCesConnection) -> None:
            await ces.wait_for(_is_event("session_start"))
            await ces.send({"endSession": {"metadata": {}}})
            await ces.closed.wait()

        async with FakeCesServer(script) as server:
            await self._start_call(server)
            loop = asyncio.get_running_loop()
            started = loop.time()
            disconnect = await self._wait_for_disconnect()
            elapsed_s = loop.time() - started
            # The adapter closes the CES socket itself (TC-7.1).
            await asyncio.wait_for(
                server.connections[0].closed.wait(), _TIMEOUT_S
            )

        self.assertEqual(disconnect["parameters"]["reason"], "completed")
        self.assertLess(elapsed_s, 2.0)

    async def test_close_without_end_session_disconnects(self):
        async def script(_, ces: FakeCesConnection) -> None:
            await ces.wait_for(_is_event("session_start"))
            await ces.connection.close()

        async with FakeCesServer(script) as server:
            await self._start_call(server)
            disconnect = await self._wait_for_disconnect()

        self.assertEqual(
            disconnect["parameters"],
            {"reason": "error", "info": "CES WS Closed: 1000"},
        )

    async def test_transient_open_failure_is_retried(self):
        async def script(_, ces: FakeCesConnection) -> None:
            await ces.wait_for(_is_event("session_start"))
            await ces.closed.wait()

        async with FakeCesServer(script, reject_first=1) as server:
            await self._start_call(server)
            await _wait_until(lambda: self.genesys.messages("opened"))

        self.assertEqual(server.rejected, 1)
        self.assertEqual(len(server.connections), 1)

    async def test_pause_sends_silence_until_resumed(self):
        silence = base64.b64encode(b"\xff" * 160).decode()

        def is_silence(message: dict) -> bool:
            return message.get("realtimeInput", {}).get("audio") == silence

        async def script(_, ces: FakeCesConnection) -> None:
            await ces.closed.wait()

        async with FakeCesServer(script) as server:
            await self._start_call(server)
            await _wait_until(lambda: self.genesys.messages("opened"))
            self.genesys.push(
                {"type": "paused", "seq": 2, "id": "genesys-session-1"}
            )
            await asyncio.sleep(0.3)
            self.genesys.push(
                {"type": "resumed", "seq": 3, "id": "genesys-session-1"}
            )
            await asyncio.sleep(0.1)
            frames_at_resume = sum(
                map(is_silence, server.connections[0].received)
            )
            await asyncio.sleep(0.2)
            frames_later = sum(map(is_silence, server.connections[0].received))

        # About 15 frames in 300 ms; none after `resumed`.
        self.assertGreaterEqual(frames_at_resume, 10)
        self.assertEqual(frames_later, frames_at_resume)

    async def test_no_audio_after_closed(self):
        async def script(_, ces: FakeCesConnection) -> None:
            await ces.wait_for(_is_event("session_start"))
            for _ in range(10):
                await ces.send_audio(8000)
            await ces.closed.wait()

        async with FakeCesServer(script) as server:
            await self._start_call(server)
            await _wait_until(lambda: self.genesys.audio_bytes() > 0)
            self.genesys.push(
                {"type": "close", "seq": 2, "id": "genesys-session-1"}
            )
            await _wait_until(lambda: self.genesys.messages("closed"))
            await asyncio.sleep(0.6)  # Two pacer intervals.
            await asyncio.wait_for(
                server.connections[0].closed.wait(), _TIMEOUT_S
            )

        sent = self.genesys.sent
        closed_index = next(
            i
            for i, m in enumerate(sent)
            if isinstance(m, str) and json.loads(m)["type"] == "closed"
        )
        self.assertFalse(
            [m for m in sent[closed_index:] if isinstance(m, bytes)]
        )

    async def test_stereo_offer_accepts_external_channel_only(self):
        async def script(_, ces: FakeCesConnection) -> None:
            await ces.closed.wait()

        async with FakeCesServer(script) as server:
            self._use_backend_url(server.base_url)
            self._handler_task = asyncio.create_task(
                self.handler.handle_connection()
            )
            message = _open_message(
                {"_deployment_id": _DEPLOYMENT_ID}, _CONVERSATION_ID
            )
            message["parameters"]["media"] = [
                {
                    "type": "audio",
                    "format": "PCMU",
                    "channels": ["external", "internal"],
                    "rate": 8000,
                },
            ]
            self.genesys.push(message)
            await _wait_until(lambda: self.genesys.messages("opened"))

        opened = self.genesys.messages("opened")[0]
        self.assertEqual(
            opened["parameters"]["media"],
            [
                {
                    "type": "audio",
                    "format": "PCMU",
                    "channels": ["external"],
                    "rate": 8000,
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
