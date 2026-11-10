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

"""One call between Genesys and a virtual-agent backend.

`CallSession` owns everything about a call that doesn't depend on the
AudioHook wire protocol or on the backend's transport: the session-ID
policy, agent variables, the backend listen loop, the outbound pacer,
rollover and teardown.
"""

import asyncio
from collections.abc import Mapping
import json
import logging
from typing import Any, Protocol

from src import pacer
from src import redaction
from src import settings
from src.backends import base
from src.backends import factory

logger = logging.getLogger(__name__)

# How long to keep reading after the agent ends the session.
POST_END_SESSION_RECV_TIMEOUT_S = 5.0
# How long a caller hang-up waits for the backend to finish its turn.
HANGUP_WRAPUP_TIMEOUT_S = 5.0
_HANGUP_SETTLE_S = 0.5
_ROLLOVER_DRAIN_S = 0.5
_REHYDRATION_PAYLOAD = '{"reconnected": true}'
# `projects/P/locations/L/apps/A/deployments/D` has 8 path segments.
_DEPLOYMENT_PATH_SEGMENTS = 8


class GenesysLink(Protocol):
    """What a call session needs from the Genesys connection."""

    @property
    def disconnect_initiated(self) -> bool:
        """Whether a disconnect or close is already in progress."""

    async def send_disconnect(
        self,
        reason: str = "normal",
        info: str | None = None,
        output_variables: Mapping[str, Any] | None = None,
    ) -> None:
        """Sends an AudioHook `disconnect` message."""

    async def send_error_report(
        self,
        error_type: str,
        error_message: str,
        source: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        """Sends an error report to Genesys."""

    async def send_audio(self, chunk: bytes) -> None:
        """Sends one binary audio frame to Genesys."""

    def audio_sink_open(self) -> bool:
        """Returns whether audio can still be sent to Genesys."""


def agent_id_from_deployment(deployment_id: str) -> str | None:
    """Derives the agent (app) name from a deployment name.

    Args:
      deployment_id: `projects/P/locations/L/apps/A/deployments/D`.

    Returns:
      `projects/P/locations/L/apps/A`, or None if the name is malformed.
    """
    parts = deployment_id.split("/")
    if len(parts) == _DEPLOYMENT_PATH_SEGMENTS and parts[6] == "deployments":
        return "/".join(parts[:6])
    return None


class CallSession:
    """Lifecycle of one call against a virtual-agent backend."""

    def __init__(
        self,
        genesys: GenesysLink,
        adapter_session_id: str,
        backend: base.VirtualAgentBackend | None = None,
    ):
        """Initializes the session; nothing is opened until `start`.

        Args:
          genesys: The Genesys connection for this call.
          adapter_session_id: UUID the adapter assigned to this connection.
          backend: Backend to use; by default one is created by the factory.
        """
        self._genesys = genesys
        self.adapter_session_id = adapter_session_id
        self.genesys_conv_id: str | None = None
        self.session_id: str | None = None
        self.agent_id: str | None = None
        self.deployment_id: str | None = None
        self.initial_message: Any | None = None
        self.agent_variables: dict[str, Any] = {}
        self.endsession_received = False
        # Set when the backend reports end of session, a close or an error.
        self.backend_data_received = asyncio.Event()
        self._backend = backend or factory.create_backend(self.log_extra)
        self._pacer = pacer.OutboundPacer(
            send=genesys.send_audio,
            sink_open=genesys.audio_sink_open,
            log_extra=self.log_extra,
            on_error=self._on_pacer_error,
        )
        self._start: base.SessionStart | None = None
        self._final_params: dict[str, Any] = {}
        self._inbound: asyncio.Queue[bytes] = asyncio.Queue()
        self._listen_task: asyncio.Task[None] | None = None
        self._rollover_task: asyncio.Task[None] | None = None
        self._reconnecting = False
        self._generation = 0
        self._closed = False

    @property
    def connectivity_mode(self) -> str:
        """The backend's connectivity mode."""
        return self._backend.mode

    @property
    def agent_session_id(self) -> str | None:
        """CES session name or Dialogflow conversation name."""
        return self._backend.agent_session_id

    @property
    def ces_session_id(self) -> str | None:
        """CES session name in `ces` mode; kept for log compatibility."""
        if self._backend.mode != "ces":
            return None
        return self._backend.agent_session_id

    @property
    def pacer(self) -> pacer.OutboundPacer:
        """The outbound pacer."""
        return self._pacer

    def log_extra(
        self, log_type: str, data: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        """Builds the structured-logging `extra` dict for this call.

        Args:
          log_type: Value of the `log_type` field.
          data: Extra fields; values are redacted.

        Returns:
          The `extra` dict.
        """
        extra = {
            "log_type": log_type,
            "adapter_session_id": self.adapter_session_id,
            "genesys_conv_id": self.genesys_conv_id,
            "ces_session_id": self.ces_session_id,
            "agent_session_id": self.agent_session_id,
            "connectivity_mode": self.connectivity_mode,
        }
        if data:
            extra.update(redaction.redact(dict(data)))
        return extra

    def is_connected(self) -> bool:
        """Returns whether the backend transport is open."""
        return self._backend.is_connected()

    async def start(
        self,
        input_variables: Mapping[str, Any] | None,
        conversation_id: str | None,
    ) -> bool:
        """Resolves the call parameters and opens the backend.

        Args:
          input_variables: Genesys `inputVariables` from the `open` message.
          conversation_id: Genesys `conversationId`.

        Returns:
          True if the backend is open and the call is running. On False a
          disconnect has already been sent where appropriate.
        """
        self.genesys_conv_id = conversation_id
        if not await self._resolve_parameters(input_variables or {}):
            return False
        self._resolve_session_id(input_variables or {})
        if not self.agent_id:
            logger.error(
                "Missing _deployment_id or _agent_id",
                extra=self.log_extra(
                    "genesys_config_error", {"input_variables": input_variables}
                ),
            )
            await self._genesys.send_disconnect(
                "error",
                "Missing required parameter: _agent_id or _deployment_id",
            )
            return False
        self._start = base.SessionStart(
            agent_id=self.agent_id,
            deployment_id=self.deployment_id,
            session_id=self.session_id,
            variables=self.agent_variables,
            initial_message=self.initial_message,
        )
        if not await self._open_backend():
            logger.error(
                "CES connection failed, stopping setup",
                extra=self.log_extra("genesys_config_error"),
            )
            return False
        try:
            self._listen_task = asyncio.create_task(self._listen())
            self._pacer.start()
        except Exception as err:  # pylint: disable=broad-exception-caught
            logger.error(
                "Error creating CES listener or pacer tasks",
                exc_info=True,
                extra=self.log_extra("genesys_ces_task_error"),
            )
            await self._genesys.send_disconnect(
                "error", f"Task creation Error: {err}"
            )
            return False
        return True

    async def _resolve_parameters(
        self, input_variables: Mapping[str, Any]
    ) -> bool:
        """Reads agent, kickstart and variables from the input variables."""
        if "_deployment_id" in input_variables:
            self.deployment_id = input_variables["_deployment_id"]
            self.agent_id = agent_id_from_deployment(self.deployment_id)
            if self.agent_id is None:
                logger.error(
                    "Invalid _deployment_id format",
                    extra=self.log_extra(
                        "genesys_config_error",
                        {"deployment_id": self.deployment_id},
                    ),
                )
                await self._genesys.send_disconnect(
                    "error", "Invalid _deployment_id format"
                )
                return False
        elif "_agent_id" in input_variables:
            self.agent_id = input_variables["_agent_id"]
        if "_initial_message" in input_variables:
            raw_message = input_variables["_initial_message"]
            try:
                self.initial_message = json.loads(raw_message)
            except json.JSONDecodeError:
                logger.warning(
                    "Failed to JSON decode _initial_message: %s",
                    raw_message,
                    exc_info=True,
                    extra=self.log_extra("genesys_config_warning"),
                )
                self.initial_message = raw_message
        if input_variables:
            self.agent_variables = {
                key: value
                for key, value in input_variables.items()
                if not key.startswith("_")
            }
            self.agent_variables["adapterSessionId"] = self.adapter_session_id
        return True

    def _resolve_session_id(self, input_variables: Mapping[str, Any]) -> None:
        """Applies the session-ID policy.

        `FORCE_ADAPTER_SESSION_ID` wins; otherwise `_session_id`, then the
        Genesys conversation ID, then the adapter session ID. A
        `_session_id` also replaces the conversation ID used in logs.
        """
        force = settings.settings.force_adapter_session_id
        if "_session_id" in input_variables and not force:
            self.session_id = input_variables["_session_id"]
            self.genesys_conv_id = self.session_id
        if force:
            self.session_id = self.adapter_session_id
        elif not self.session_id:
            self.session_id = self.genesys_conv_id or self.adapter_session_id

    async def _open_backend(
        self, rehydration_payload: str | None = None
    ) -> bool:
        try:
            await self._backend.open(
                self._start, rehydration_payload=rehydration_payload
            )
        except base.BackendOpenError as err:
            if not self._genesys.disconnect_initiated:
                await self._genesys.send_disconnect(
                    "error", info=f"CES Connection/Config Error: {err}"
                )
            return False
        return True

    async def send_audio(self, chunk: bytes) -> None:
        """Forwards caller audio, buffering it during a rollover.

        Args:
          chunk: PCMU audio at 8 kHz.
        """
        if self._reconnecting:
            self._inbound.put_nowait(chunk)
            return
        try:
            await self._backend.send_audio(chunk)
        except base.BackendSendError as err:
            await self._genesys.send_disconnect(
                "error", info=f"CES Send Audio Error: {err}"
            )

    async def send_dtmf(self, digit: str) -> None:
        """Forwards a DTMF digit; failures become Genesys error reports.

        Args:
          digit: The digit, as received from Genesys.
        """
        try:
            await self._backend.send_dtmf(digit)
        except base.BackendSendError as err:
            await self._genesys.send_error_report(
                error_type=err.error_type,
                error_message=str(err),
                source="CESWS.send_dtmf",
                details=err.details,
            )

    async def _listen(self) -> None:
        """Reads backend events until the stream ends, then finalizes."""
        generation = self._generation
        # Read until the backend reports the end of the stream: messages
        # that arrived just before a close are still delivered first.
        while True:
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "CES WS: Waiting for message...",
                    extra=self.log_extra("ces_recv_wait"),
                )
            if self.endsession_received:
                try:
                    event = await asyncio.wait_for(
                        self._backend.receive(),
                        timeout=POST_END_SESSION_RECV_TIMEOUT_S,
                    )
                except asyncio.TimeoutError:
                    logger.info(
                        "Timeout waiting for audio after endSession. Exiting"
                        " listen loop.",
                        extra=self.log_extra("ces_listen_timeout"),
                    )
                    break
            else:
                event = await self._backend.receive()
            if not await self._handle_event(event):
                break
        if generation != self._generation:
            return  # A rollover replaced this stream; nothing to finalize.
        logger.info(
            "Exited listen loop, waiting for pacer to drain...",
            extra=self.log_extra("ces_listen_exit"),
        )
        await self._pacer.wait_drained()
        await self.close()
        if self.endsession_received and not self._genesys.disconnect_initiated:
            await self._genesys.send_disconnect(
                "completed",
                info="Session has ended successfully in CES",
                output_variables=self._final_params,
            )

    async def _handle_event(self, event: base.BackendEvent | None) -> bool:
        """Applies one backend event.

        Args:
          event: The event, or None for messages that need no action.

        Returns:
          Whether the listen loop should continue.
        """
        if isinstance(event, base.BargeIn):
            self._pacer.clear_for_barge_in()
        elif isinstance(event, base.AudioOut):
            self._pacer.enqueue(event.audio)
        elif isinstance(event, base.EndOfInteraction):
            self._on_end_of_interaction(event)
        elif isinstance(event, base.Rollover):
            self._rollover_task = asyncio.create_task(self._rollover())
        elif isinstance(event, base.StreamEnded):
            await self._on_stream_ended(event)
            self.backend_data_received.set()
            return False
        elif isinstance(event, base.BackendError):
            if not self._genesys.disconnect_initiated:
                await self._genesys.send_disconnect("error", info=event.message)
            self.backend_data_received.set()
            return False
        return True

    def _on_end_of_interaction(self, event: base.EndOfInteraction) -> None:
        self.endsession_received = True
        self._pacer.end_of_stream = True
        if not self._genesys.disconnect_initiated:
            self._final_params = dict(event.output_variables)
            logger.info(
                "CES endSession: Waiting for final audio...",
                extra=self.log_extra("ces_endsession_wait_audio"),
            )
        self.backend_data_received.set()

    async def _on_stream_ended(self, event: base.StreamEnded) -> None:
        data = {"code": event.code, "reason": event.reason, "exc": event.detail}
        if self._genesys.disconnect_initiated or event.clean:
            logger.info(
                "CES WS connection closed cleanly",
                extra=self.log_extra("ces_connection_closed", data),
            )
            return
        logger.warning(
            "CES WS connection closed unexpectedly",
            extra=self.log_extra("ces_connection_closed", data),
        )
        if event.sys_error:
            await self._genesys.send_disconnect(
                "normal", output_variables={"SYS_ERROR": event.sys_error}
            )
        else:
            await self._genesys.send_disconnect(
                "error", info=f"CES WS Closed: {event.code}"
            )

    async def _rollover(self) -> None:
        """Moves the call to a new backend stream after a GoAway."""
        logger.info(
            "Starting goAway reconnect and rehydrate",
            extra=self.log_extra("ces_reconnect_start"),
        )
        self._reconnecting = True
        self._generation += 1
        await asyncio.sleep(_ROLLOVER_DRAIN_S)  # Let trailing output arrive.
        try:
            await self._backend.close()
        except Exception as err:  # pylint: disable=broad-exception-caught
            logger.warning(
                "Error closing dying CES socket: %s",
                err,
                extra=self.log_extra("ces_reconnect_close_error"),
            )
        await _cancel_and_wait(self._listen_task)
        await self._pacer.cancel_task()
        self.endsession_received = False
        self._pacer.end_of_stream = False
        if not await self._open_backend(_REHYDRATION_PAYLOAD):
            logger.error(
                "Failed to re-establish CES connection during rehydration",
                extra=self.log_extra("ces_reconnect_fail"),
            )
            await self._genesys.send_disconnect(
                "error", info="CES Reconnection Failed"
            )
            return
        self._listen_task = asyncio.create_task(self._listen())
        self._pacer.start()
        self._reconnecting = False
        flushed = await self._flush_inbound()
        logger.info(
            "Reconnected and flushed %s audio chunks",
            flushed,
            extra=self.log_extra("ces_reconnect_success"),
        )

    async def _flush_inbound(self) -> int:
        flushed = 0
        while not self._inbound.empty():
            chunk = self._inbound.get_nowait()
            try:
                await self._backend.send_audio(chunk)
            except base.BackendSendError:
                break  # Already logged by the backend.
            flushed += 1
        return flushed

    async def hangup(self) -> None:
        """Ends the call after the caller hung up or the socket dropped.

        Sends the disconnect event, waits up to `HANGUP_WRAPUP_TIMEOUT_S`
        for the backend to finish its turn, then closes.
        """
        logger.info(
            "Signalling CES and waiting up to %ss for session to end...",
            HANGUP_WRAPUP_TIMEOUT_S,
            extra=self.log_extra("genesys_recv_close_start"),
        )
        if self._backend.is_connected() and not self.endsession_received:
            await self._signal_disconnect_and_wait()
        else:
            logger.warning(
                "CES WS not connected, cannot send disconnect event",
                extra=self.log_extra("genesys_ces_event_skip"),
            )
        logger.info(
            "Forcefully closing CES WS as per teardown protocol.",
            extra=self.log_extra("genesys_close_cesHandler_force"),
        )
        await self.close()

    async def _signal_disconnect_and_wait(self) -> None:
        event_name = settings.settings.disconnect_event_name
        logger.info(
            "Sending '%s' event and clientHalfClose to CES",
            event_name,
            extra=self.log_extra("genesys_send_ces_event"),
        )
        try:
            await self._backend.send_event(event_name)
            await self._backend.send_half_close()
            await asyncio.wait_for(
                self.backend_data_received.wait(),
                timeout=HANGUP_WRAPUP_TIMEOUT_S,
            )
            await asyncio.sleep(_HANGUP_SETTLE_S)
            logger.info(
                "CES finished processing disconnect event.",
                extra=self.log_extra("genesys_close_ces_complete"),
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Timeout waiting for CES to process event after %ss."
                " Proceeding with close.",
                HANGUP_WRAPUP_TIMEOUT_S,
                extra=self.log_extra("genesys_close_timeout"),
            )
        except Exception as err:  # pylint: disable=broad-exception-caught
            logger.error(
                "Error waiting for CES event: %s",
                err,
                exc_info=True,
                extra=self.log_extra("genesys_close_error"),
            )

    async def _on_pacer_error(self, info: str) -> None:
        if not self._genesys.disconnect_initiated:
            await self._genesys.send_disconnect("error", info=info)

    async def close(self) -> None:
        """Stops audio, cancels tasks and closes the backend; idempotent."""
        if self._closed:
            return
        self._closed = True
        await self._pacer.stop()
        self._clear_inbound()
        current_task = asyncio.current_task()
        for task in (self._listen_task, self._rollover_task):
            if task is not current_task:
                await _cancel_and_wait(task)
        await self._backend.close()

    def _clear_inbound(self) -> None:
        cleared = 0
        while not self._inbound.empty():
            self._inbound.get_nowait()
            cleared += 1
        if cleared:
            logger.info(
                "Audio INBOUND queue cleared: discarded %s chunks",
                cleared,
                extra=self.log_extra(
                    "ces_inbound_queue_clear", {"cleared_count": cleared}
                ),
            )
        else:
            logger.info(
                "Audio INBOUND queue is empty",
                extra=self.log_extra("ces_inbound_queue_clear"),
            )


async def _cancel_and_wait(task: asyncio.Task[None] | None) -> None:
    """Cancels a task and waits for it, ignoring its cancellation."""
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
