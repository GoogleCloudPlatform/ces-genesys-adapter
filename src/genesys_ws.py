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

"""Genesys AudioHook protocol handling for one WebSocket connection.

`GenesysWS` speaks the AudioHook wire protocol (open/opened, ping/pong,
dtmf, close/closed, disconnect, sequence numbers). Everything about the
call itself is delegated to `call_session.CallSession`.
"""

import asyncio
from collections.abc import Coroutine, Mapping
import json
import logging
from typing import Any

from starlette import websockets as starlette_websockets
import websockets
from websockets import protocol

from src import call_session
from src import redaction

logger = logging.getLogger(__name__)

PROBE_CONVERSATION_ID = "00000000-0000-0000-0000-000000000000"
_EXPECTED_CLOSE_CODES = (1000, 1001, 1005)
_ABNORMAL_CLOSE_CODE = 1006

# Keeps detached tasks referenced until they finish.
_background_tasks: set[asyncio.Task[None]] = set()


def _create_detached_task(
    coro: Coroutine[Any, Any, None],
) -> asyncio.Task[None]:
    """Runs a coroutine as a task that outlives the connection handler."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


def _close_code_and_reason(
    err: websockets.exceptions.ConnectionClosed,
) -> tuple[int, str]:
    close_frame = err.rcvd
    if close_frame is None:
        return _ABNORMAL_CLOSE_CODE, ""
    return close_frame.code, close_frame.reason


def select_media(
    offered_media: list[Mapping[str, Any]],
) -> Mapping[str, Any] | None:
    """Returns the first offered PCMU 8 kHz audio media, if any.

    Args:
      offered_media: `parameters.media` from the Genesys `open` message.
    """
    for media in offered_media:
        if (
            media.get("type") == "audio"
            and media.get("format") == "PCMU"
            and media.get("rate") == 8000
        ):
            return media
    return None


class GenesysWS:
    """AudioHook server side of one Genesys connection."""

    def __init__(self, websocket: Any, adapter_session_id: str):
        """Initializes the handler.

        Args:
          websocket: The Genesys socket (websockets `Protocol` interface).
          adapter_session_id: UUID the adapter assigned to this connection.
        """
        self.websocket = websocket
        self.adapter_session_id = adapter_session_id
        self.call: call_session.CallSession | None = None
        self.last_server_sequence_number = 0
        self.last_client_sequence_number = 0
        self.client_session_id: str | None = None
        self.conversation_id: str | None = None
        self.disconnect_initiated = False
        self.is_probe = False
        self._teardown_task: asyncio.Task[None] | None = None

    def _log_extra(
        self, log_type: str, data: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        extra = {
            "log_type": log_type,
            "adapter_session_id": self.adapter_session_id,
            "genesys_session_id": self.client_session_id,
            "genesys_conv_id": self.conversation_id,
            "server_seq": self.last_server_sequence_number,
            "client_seq": self.last_client_sequence_number,
            "ces_session_id": self.call.ces_session_id if self.call else None,
            "agent_session_id": (
                self.call.agent_session_id if self.call else None
            ),
            "connectivity_mode": (
                self.call.connectivity_mode if self.call else None
            ),
        }
        if data:
            extra.update(redaction.redact(dict(data)))
        return extra

    async def handle_connection(self) -> None:
        """Reads Genesys messages until the connection ends."""
        self.call = call_session.CallSession(self, self.adapter_session_id)
        try:
            logger.debug(
                "Genesys WS: Waiting for message...",
                extra=self._log_extra("genesys_recv_wait"),
            )
            async for message in self.websocket:
                if isinstance(message, str):
                    await self.handle_text_message(message)
                elif isinstance(message, bytes):
                    await self.handle_binary_message(message)
        except websockets.exceptions.ConnectionClosed as err:
            await self._on_connection_closed(err)
        except Exception as err:  # pylint: disable=broad-exception-caught
            logger.error(
                "Error in Genesys WebSocket handler",
                exc_info=True,
                extra=self._log_extra("genesys_error"),
            )
            if not self.disconnect_initiated:
                await self.send_disconnect(
                    "error", info=f"WebSocket Error: {err}"
                )
        finally:
            self._on_connection_finished()

    async def _on_connection_closed(
        self, err: websockets.exceptions.ConnectionClosed
    ) -> None:
        code, reason = _close_code_and_reason(err)
        if self.disconnect_initiated or code in _EXPECTED_CLOSE_CODES:
            logger.info(
                "Genesys WebSocket closed as expected.",
                extra=self._log_extra(
                    "genesys_connection_closed",
                    {"code": code, "reason": reason},
                ),
            )
            return
        logger.warning(
            "Genesys WebSocket closed unexpectedly mid-session.",
            extra=self._log_extra(
                "genesys_connection_closed",
                {"code": code, "reason": reason, "exc": str(err)},
            ),
        )
        if self.call and self.call.is_connected():
            await self.send_disconnect(
                "error", info=f"Genesys WS ConnectionClosedError: {err}"
            )

    def _on_connection_finished(self) -> None:
        logger.info(
            "Genesys connection loop finished. Cleaning up CES connection.",
            extra=self._log_extra("genesys_connection_cleanup"),
        )
        if self._teardown_task and not self._teardown_task.done():
            logger.info(
                "Background teardown task running asynchronously. Exiting"
                " connection loop.",
                extra=self._log_extra("genesys_teardown_running"),
            )
        if not self.disconnect_initiated:
            # The client went away without `close`: tear down in the
            # background so the connection slot is released immediately.
            logger.info(
                "Abrupt client disconnect detected. Triggering graceful"
                " teardown in background.",
                extra=self._log_extra("genesys_abrupt_teardown"),
            )
            self.disconnect_initiated = True
            self._teardown_task = _create_detached_task(self.call.hangup())

    async def handle_text_message(self, message: str) -> None:
        """Handles one AudioHook text message.

        Args:
          message: The raw JSON message.
        """
        logger.info(
            "Received text message from Genesys",
            extra=self._log_extra(
                "genesys_recv", {"data": redaction.redact(message)}
            ),
        )
        try:
            data = json.loads(message)
        except json.JSONDecodeError:
            logger.error(
                "Error decoding JSON from Genesys",
                extra=self._log_extra(
                    "genesys_json_decode_error", {"message": message}
                ),
            )
            await self.send_disconnect("error", "Invalid JSON received")
            return
        message_type = data.get("type")
        logger.info(
            "Received Genesys message",
            extra=self._log_extra(
                "genesys_recv_parsed", {"message_type": message_type}
            ),
        )
        self.last_client_sequence_number = data.get("seq")
        self.client_session_id = data.get("id")
        await self._dispatch(message_type, data)

    async def _dispatch(self, message_type: str, data: dict[str, Any]) -> None:
        if message_type == "open":
            await self._on_open(data.get("parameters", {}))
        elif message_type == "ping":
            logger.info(
                "Received 'ping' message from Genesys",
                extra=self._log_extra("genesys_recv_ping"),
            )
            await self.send_message(self._reply("pong"))
        elif message_type in ("playback_started", "playback_completed"):
            logger.info(
                "Received %s from Genesys",
                message_type.replace("_", "-"),
                extra=self._log_extra(
                    f"genesys_recv_{message_type}", {"data": data}
                ),
            )
        elif message_type == "dtmf":
            await self._on_dtmf(data)
        elif message_type == "close":
            await self._on_close()
        elif message_type == "update":
            logger.info(
                "Received update message from Genesys",
                extra=self._log_extra("genesys_recv_update", {"data": data}),
            )
        else:
            logger.info(
                "Received unhandled message type from Genesys",
                extra=self._log_extra(
                    "genesys_recv_unhandled",
                    {"message_type": message_type, "data": data},
                ),
            )

    def _reply(
        self, message_type: str, parameters: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        reply = {
            "type": message_type,
            "version": "2",
            "id": self.client_session_id,
            "clientseq": self.last_client_sequence_number,
        }
        if parameters is not None:
            reply["parameters"] = dict(parameters)
        return reply

    async def _on_open(self, parameters: Mapping[str, Any]) -> None:
        self.conversation_id = parameters.get("conversationId")
        if self.conversation_id == PROBE_CONVERSATION_ID:
            self.is_probe = True
            logger.info(
                "Connection Probe detected (Null UUID). Skipping CES"
                " connection.",
                extra=self._log_extra("genesys_probe"),
            )
        else:
            started = await self.call.start(
                parameters.get("inputVariables"), self.conversation_id
            )
            self.conversation_id = self.call.genesys_conv_id
            if not started:
                return
            logger.info(
                "Genesys session opened", extra=self._log_extra("genesys_open")
            )
        self._log_custom_config(parameters.get("customConfig"))
        selected_media = select_media(parameters.get("media", []))
        if not selected_media:
            await self.send_disconnect(
                "error", "No compatible audio media offered."
            )
            return
        opened_message = self._reply(
            "opened", {"startPaused": False, "media": [selected_media]}
        )
        logger.info(
            "Sending 'opened' message to Genesys",
            extra=self._log_extra(
                "genesys_send_opened", {"opened_msg": opened_message}
            ),
        )
        await self.send_message(opened_message)

    def _log_custom_config(self, custom_config_str: str | None) -> None:
        if not custom_config_str:
            return
        logger.info(
            "Found customConfig from Genesys",
            extra=self._log_extra(
                "genesys_custom_config",
                {"custom_config_str": custom_config_str},
            ),
        )
        try:
            custom_config = json.loads(custom_config_str)
        except json.JSONDecodeError:
            logger.error(
                "Error decoding customConfig JSON",
                extra=self._log_extra(
                    "genesys_custom_config_error",
                    {"custom_config": custom_config_str},
                ),
            )
            return
        if not isinstance(custom_config, dict):
            logger.warning(
                "Custom config is not a dict",
                extra=self._log_extra(
                    "genesys_custom_config", {"custom_config": custom_config}
                ),
            )
            return
        for key, value in custom_config.items():
            logger.info(
                "Custom config item",
                extra=self._log_extra(
                    "genesys_custom_config", {"key": key, "value": value}
                ),
            )

    async def _on_dtmf(self, data: Mapping[str, Any]) -> None:
        logger.info(
            "Received 'dtmf' message from Genesys",
            extra=self._log_extra("genesys_recv_dtmf"),
        )
        digit = data.get("parameters", {}).get("digit")
        if not digit:
            logger.warning(
                "Received DTMF message without digit",
                extra=self._log_extra(
                    "genesys_recv_dtmf_missing", {"data": data}
                ),
            )
            return
        logger.info(
            "Received DTMF from Genesys",
            extra=self._log_extra(
                "genesys_recv_dtmf", {"digit": redaction.redact_value(digit)}
            ),
        )
        await self.call.send_dtmf(digit)

    async def _on_close(self) -> None:
        logger.info(
            "Received 'close' message from Genesys",
            extra=self._log_extra("genesys_recv_closed"),
        )
        if self.disconnect_initiated:
            logger.info(
                "Disconnect already initiated by adapter, sending 'closed'"
                " immediately.",
                extra=self._log_extra("genesys_close_ack"),
            )
        else:
            self.disconnect_initiated = True
            # Tear down in the background to free the Cloud Run connection.
            self._teardown_task = _create_detached_task(self.call.hangup())
        closed_message = self._reply("closed", {})
        logger.info(
            "Sending 'closed' message to Genesys",
            extra=self._log_extra(
                "genesys_send_closed",
                {"closed_message": redaction.redact(closed_message)},
            ),
        )
        await self.send_message(closed_message)

    async def send_disconnect(
        self,
        reason: str = "normal",
        info: str | None = None,
        output_variables: Mapping[str, Any] | None = None,
    ) -> None:
        """Stops the call and sends an AudioHook `disconnect` message.

        Args:
          reason: Disconnect reason.
          info: Optional detail for `error` disconnects.
          output_variables: Variables returned to Genesys; non-string values
            are JSON-encoded.
        """
        if self.disconnect_initiated:
            logger.info(
                "Disconnect already in progress, skipping duplicate call",
                extra=self._log_extra("genesys_disconnect_duplicate"),
            )
            return
        self.disconnect_initiated = True
        logger.info(
            "Preparing to send disconnect",
            extra=self._log_extra(
                "genesys_disconnect_start",
                {
                    "reason": reason,
                    "info": info,
                    "output_variables": output_variables,
                },
            ),
        )
        parameters: dict[str, Any] = {"reason": reason}
        if info:
            parameters["info"] = info
        if output_variables:
            parameters["outputVariables"] = {
                key: (
                    value
                    if isinstance(value, str)
                    else json.dumps(value, default=str)
                )
                for key, value in output_variables.items()
            }
        disconnect_message = self._reply("disconnect", parameters)
        if self.call:
            logger.info(
                "Stopping audio and clearing queues...",
                extra=self._log_extra("genesys_disconnect_audio_drain"),
            )
            await self.call.close()
            logger.info(
                "Audio queues cleared and tasks cleaned up.",
                extra=self._log_extra("genesys_disconnect_audio_drain"),
            )
        logger.info(
            "Sending disconnect message to Genesys",
            extra=self._log_extra(
                "genesys_send_disconnect",
                {"disconnect_message": redaction.redact(disconnect_message)},
            ),
        )
        await self.send_message(disconnect_message)

    def get_next_server_sequence_number(self) -> int:
        """Increments and returns the server sequence number."""
        self.last_server_sequence_number += 1
        return self.last_server_sequence_number

    async def handle_binary_message(self, message: bytes) -> None:
        """Forwards caller audio to the call.

        Args:
          message: PCMU audio at 8 kHz.
        """
        if self.disconnect_initiated:
            logger.info(
                "GenesysWS: Ignoring binary message during disconnect",
                extra=self._log_extra("genesys_ignore_binary"),
            )
            return
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "GenesysWS: Received binary message",
                extra=self._log_extra(
                    "genesys_recv_binary", {"audio_size": len(message)}
                ),
            )
        if self.call:
            await self.call.send_audio(message)

    async def send_audio(self, chunk: bytes) -> None:
        """Sends one binary audio frame to Genesys (used by the pacer).

        Args:
          chunk: PCMU audio at 8 kHz.
        """
        await self.websocket.send(chunk)

    def audio_sink_open(self) -> bool:
        """Returns whether audio can still be sent to Genesys."""
        return (
            self.websocket is not None
            and self.websocket.state != protocol.State.CLOSED
        )

    async def send_message(self, message: dict[str, Any]) -> None:
        """Sends a JSON message to Genesys with the next sequence number.

        Args:
          message: The message; `seq` is added.

        Raises:
          Exception: Unexpected send errors are logged and re-raised.
            Connection drops are logged and swallowed.
        """
        if not self.websocket or self.websocket.state != protocol.State.OPEN:
            logger.debug(
                "Attempted to send message on non-open WebSocket",
                extra=self._log_extra(
                    "genesys_send_error", {"payload": redaction.redact(message)}
                ),
            )
            return
        try:
            message["seq"] = self.get_next_server_sequence_number()
            logger.debug(
                "Sending message to Genesys",
                extra=self._log_extra(
                    "genesys_send", {"payload": redaction.redact(message)}
                ),
            )
            await self.websocket.send(json.dumps(message))
        except (
            RuntimeError,
            websockets.exceptions.ConnectionClosed,
            starlette_websockets.WebSocketDisconnect,
        ) as err:
            logger.warning(
                "Connection dropped during send to Genesys (client"
                " disconnected abruptly): %s",
                err,
                extra=self._log_extra("genesys_send_dropped"),
            )
        except Exception:
            logger.error(
                "Error sending message to Genesys",
                exc_info=True,
                extra=self._log_extra(
                    "genesys_send_error", {"payload": redaction.redact(message)}
                ),
            )
            raise

    async def send_error_report(
        self,
        error_type: str,
        error_message: str,
        source: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        """Sends an error report to Genesys unless a disconnect is underway.

        Args:
          error_type: Short error category.
          error_message: Human-readable description.
          source: Where the error happened.
          details: Extra details.
        """
        payload: dict[str, Any] = {
            "errorType": error_type,
            "errorMessage": error_message,
        }
        if source:
            payload["source"] = source
        if details:
            payload["details"] = dict(details)
        if self.disconnect_initiated:
            logger.debug(
                "Suppressing error report due to disconnect in progress.",
                extra=self._log_extra("genesys_error_report_suppress"),
            )
            return
        data_message = {
            "type": "data",
            "payload": {"type": "errorReport", "payload": payload},
        }
        try:
            await self.send_message(data_message)
        except Exception:  # pylint: disable=broad-exception-caught
            logger.error(
                "Failed to send error report to Genesys",
                exc_info=True,
                extra=self._log_extra(
                    "genesys_error_report_failed", {"errorType": error_type}
                ),
            )
            return
        logger.info(
            "Sent error report to Genesys",
            extra=self._log_extra(
                "genesys_send_error_report",
                {
                    "errorType": error_type,
                    "errorMessage": error_message,
                    "details": details,
                },
            ),
        )
