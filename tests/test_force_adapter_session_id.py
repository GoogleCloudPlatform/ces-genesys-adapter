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

"""Tests for the session-ID policy, including FORCE_ADAPTER_SESSION_ID."""

import asyncio
import dataclasses
import unittest
from unittest import mock

from src import call_session
from src import settings as settings_lib
from src.backends import base

_AGENT_ID = "projects/test-proj/locations/us/apps/app-1"
_DEPLOYMENT_ID = f"{_AGENT_ID}/deployments/dep-1"


def _settings(force: bool) -> settings_lib.Settings:
    return dataclasses.replace(
        settings_lib.settings, force_adapter_session_id=force
    )


async def _wait_forever() -> None:
    await asyncio.Event().wait()


def _make_session(
    adapter_session_id: str,
) -> tuple[call_session.CallSession, mock.AsyncMock, mock.AsyncMock]:
    """Returns a session with a mocked Genesys link and backend."""
    genesys = mock.AsyncMock()
    genesys.disconnect_initiated = False
    genesys.audio_sink_open = mock.MagicMock(return_value=True)
    backend = mock.AsyncMock()
    backend.mode = "ces"
    backend.agent_session_id = None
    backend.is_connected = mock.MagicMock(return_value=True)
    # Like a real transport, receive() waits until there is a message.
    backend.receive = mock.AsyncMock(side_effect=_wait_forever)
    session = call_session.CallSession(genesys, adapter_session_id, backend)
    return session, genesys, backend


class SessionIdPolicyTest(unittest.IsolatedAsyncioTestCase):

    async def _start(
        self, force: bool, adapter_session_id: str, input_variables: dict
    ) -> tuple[call_session.CallSession, mock.AsyncMock]:
        session, _, backend = _make_session(adapter_session_id)
        with mock.patch.object(
            call_session.settings, "settings", _settings(force)
        ):
            started = await session.start(input_variables, "genesys-conv-999")
        self.assertTrue(started)
        await session.close()
        return session, backend

    async def test_default_prefers_conversation_id(self):
        session, _ = await self._start(
            False, "adapter-uuid-1", {"_agent_id": _AGENT_ID}
        )

        self.assertEqual(session.session_id, "genesys-conv-999")
        self.assertEqual(session.genesys_conv_id, "genesys-conv-999")

    async def test_session_id_variable_overrides_conversation_id(self):
        session, _ = await self._start(
            False,
            "adapter-uuid-1",
            {"_agent_id": _AGENT_ID, "_session_id": "explicit-session-id"},
        )

        self.assertEqual(session.session_id, "explicit-session-id")
        # The conversation ID used in logs follows `_session_id`.
        self.assertEqual(session.genesys_conv_id, "explicit-session-id")

    async def test_force_adapter_session_id_overrides_session_id(self):
        session, _ = await self._start(
            True,
            "adapter-uuid-forced",
            {"_agent_id": _AGENT_ID, "_session_id": "explicit-session-id"},
        )

        self.assertEqual(session.session_id, "adapter-uuid-forced")
        # The Genesys conversationId is kept for external correlation.
        self.assertEqual(session.genesys_conv_id, "genesys-conv-999")

    async def test_backend_receives_resolved_start(self):
        _, backend = await self._start(
            False,
            "adapter-uuid-1",
            {"_deployment_id": _DEPLOYMENT_ID, "region": "west"},
        )

        start = backend.open.call_args.args[0]
        self.assertEqual(
            start,
            base.SessionStart(
                agent_id=_AGENT_ID,
                deployment_id=_DEPLOYMENT_ID,
                session_id="genesys-conv-999",
                variables={
                    "region": "west",
                    "adapterSessionId": "adapter-uuid-1",
                },
                initial_message=None,
            ),
        )


class StartValidationTest(unittest.IsolatedAsyncioTestCase):

    async def test_invalid_deployment_id_disconnects(self):
        session, genesys, backend = _make_session("adapter-uuid-1")

        started = await session.start(
            {"_deployment_id": "projects/p/apps/a"}, "conv"
        )

        self.assertFalse(started)
        genesys.send_disconnect.assert_awaited_once_with(
            "error", "Invalid _deployment_id format"
        )
        backend.open.assert_not_awaited()

    async def test_missing_agent_disconnects(self):
        session, genesys, _ = _make_session("adapter-uuid-1")

        started = await session.start({"foo": "bar"}, "conv")

        self.assertFalse(started)
        genesys.send_disconnect.assert_awaited_once_with(
            "error", "Missing required parameter: _agent_id or _deployment_id"
        )

    async def test_initial_message_json_is_decoded(self):
        session, _, _ = _make_session("adapter-uuid-1")

        await session.start(
            {"_agent_id": _AGENT_ID, "_initial_message": '"hello"'}, "conv"
        )
        await session.close()

        self.assertEqual(session.initial_message, "hello")

    async def test_initial_message_falls_back_to_raw_text(self):
        session, _, _ = _make_session("adapter-uuid-1")

        await session.start(
            {"_agent_id": _AGENT_ID, "_initial_message": "hello there"}, "conv"
        )
        await session.close()

        self.assertEqual(session.initial_message, "hello there")


class OpenRetryTest(unittest.IsolatedAsyncioTestCase):

    async def _start_with_failures(self, *failures: Exception):
        session, genesys, backend = _make_session("adapter-uuid-1")
        backend.open.side_effect = [*failures, None]
        with (
            mock.patch.object(call_session, "OPEN_RETRY_BACKOFFS_S", (0, 0)),
            mock.patch.object(call_session.health, "health_checker") as checker,
        ):
            started = await session.start({"_agent_id": _AGENT_ID}, "conv")
        await session.close()
        return started, genesys, backend, checker

    async def test_transient_failures_are_retried(self):
        transient = base.BackendOpenError("503", retryable=True)

        started, _, backend, checker = await self._start_with_failures(
            transient, transient
        )

        self.assertTrue(started)
        self.assertEqual(backend.open.await_count, 3)
        error_flags = [
            call.kwargs.get("is_error", False)
            for call in checker.record_ces_event.call_args_list
        ]
        self.assertEqual(error_flags, [True, True, False])

    async def test_permanent_failure_fails_fast(self):
        started, genesys, backend, _ = await self._start_with_failures(
            base.BackendOpenError("403 Forbidden")
        )

        self.assertFalse(started)
        self.assertEqual(backend.open.await_count, 1)
        genesys.send_disconnect.assert_awaited_once_with(
            "error", info="CES Connection/Config Error: 403 Forbidden"
        )

    async def test_retries_stop_after_two(self):
        transient = base.BackendOpenError("503", retryable=True)

        started, _, backend, _ = await self._start_with_failures(
            transient, transient, transient
        )

        self.assertFalse(started)
        self.assertEqual(backend.open.await_count, 3)


class AgentIdFromDeploymentTest(unittest.TestCase):

    def test_valid_deployment(self):
        self.assertEqual(
            call_session.agent_id_from_deployment(_DEPLOYMENT_ID), _AGENT_ID
        )

    def test_malformed_deployment(self):
        self.assertIsNone(
            call_session.agent_id_from_deployment(f"{_AGENT_ID}/versions/v1")
        )


if __name__ == "__main__":
    unittest.main()
