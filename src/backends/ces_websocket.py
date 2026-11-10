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

"""CES BidiRunSession over WebSocket (`ces` connectivity mode)."""

import base64
from collections.abc import Mapping
import json
import logging
from typing import Any

import websockets
from websockets import protocol

from src import auth
from src import redaction
from src.backends import base

logger = logging.getLogger(__name__)

CES_WS_BASE_URL = (
    "wss://ces.googleapis.com/ws/google.cloud.ces.v1.SessionService/"
    "BidiRunSession/locations/"
)
_MAX_MESSAGE_BYTES = 4 * 1024 * 1024  # Avoids message-size errors.
_OPEN_TIMEOUT_S = 30  # Survives bursts of network delay.
_CLEAN_CLOSE_CODES = (1000, 1001)
# WebSocket close code when no close frame was received.
_ABNORMAL_CLOSE_CODE = 1006


def close_sys_error(code: int, reason: str) -> str | None:
    """Maps an unexpected CES close to a `SYS_ERROR` output variable.

    Args:
      code: WebSocket close code.
      reason: WebSocket close reason.

    Returns:
      The `SYS_ERROR` value, or None if the close should be reported to
      Genesys as an error instead.
    """
    if code == 1011 and "resource_exhausted" in reason.lower():
        return "CES_RESOURCE_EXHAUSTED"
    if code == 1007:
        return "CES_UNSUPPORTED_PAYLOAD"
    return None


def is_transient_open_error(err: Exception) -> bool:
    """Returns whether a connect or setup failure is worth retrying.

    Transient: timeouts, network errors and HTTP 5xx during the handshake.
    Everything else (HTTP 4xx, protocol errors) fails fast.

    Args:
      err: The exception raised while opening the session.
    """
    if isinstance(err, websockets.exceptions.InvalidStatus):
        return err.response.status_code >= 500
    if isinstance(err, websockets.exceptions.ConnectionClosed):
        return False
    return isinstance(err, (TimeoutError, OSError))


def location_from_agent_id(agent_id: str) -> str | None:
    """Extracts the location from an agent resource name.

    Args:
      agent_id: `projects/PROJECT/locations/LOCATION/apps/APP_ID`.

    Returns:
      The location, or None if the name has no location segment.
    """
    parts = agent_id.split("/")
    try:
        return parts[parts.index("locations") + 1]
    except (ValueError, IndexError):
        return None


class CesWebSocketBackend:
    """CES `BidiRunSession` over a WebSocket carrying JSON messages."""

    mode = "ces"

    def __init__(
        self,
        log_extra: base.LogExtra,
        base_url: str = CES_WS_BASE_URL,
    ):
        """Initializes the backend.

        Args:
          log_extra: Builds the `extra` dict for log lines.
          base_url: WebSocket URL prefix; the location is appended.
        """
        self._log_extra = log_extra
        self._base_url = base_url
        self._websocket = None
        self._session_name: str | None = None

    @property
    def agent_session_id(self) -> str | None:
        """The CES session name, once a session has been opened."""
        return self._session_name

    def is_connected(self) -> bool:
        """Returns whether the WebSocket is open."""
        return (
            self._websocket is not None
            and self._websocket.state == protocol.State.OPEN
        )

    async def open(
        self,
        start: base.SessionStart,
        *,
        rehydration_payload: str | None = None,
    ) -> None:
        """Connects and sends config, variables and the kickstart turn.

        Args:
          start: Session parameters.
          rehydration_payload: If set, sent as a `historicalContexts` chunk.

        Raises:
          BackendOpenError: The connection or setup failed.
        """
        self._session_name = f"{start.agent_id}/sessions/{start.session_id}"
        location = location_from_agent_id(start.agent_id)
        if location is None:
            logger.error(
                "Could not extract location from agent_id",
                extra=self._log_extra(
                    "ces_connect_error", {"agent_id": start.agent_id}
                ),
            )
            raise base.BackendOpenError(
                "Could not extract location from agent_id"
            )
        try:
            await self._connect(location)
            await self._send_setup(start, rehydration_payload)
        except Exception as err:
            logger.error(
                "Error during CES connect/config",
                exc_info=True,
                extra=self._log_extra("ces_connect_error", None),
            )
            await self._discard_websocket()
            raise base.BackendOpenError(
                str(err), retryable=is_transient_open_error(err)
            ) from err

    async def _discard_websocket(self) -> None:
        """Closes a half-set-up socket so a retry starts clean."""
        if self._websocket is None:
            return
        try:
            await self._websocket.close()
        except Exception:  # pylint: disable=broad-exception-caught
            pass  # Best effort; the socket is being abandoned.
        self._websocket = None

    async def _connect(self, location: str) -> None:
        project_id = await auth.auth_provider.get_project_id()
        access_token = await auth.auth_provider.get_token()
        url = f"{self._base_url}{location}"
        logger.info(
            "Connecting to CES with session ID: %s",
            self._session_name,
            extra=self._log_extra("ces_connect", {"url": url}),
        )
        self._websocket = await websockets.connect(
            url,
            additional_headers={
                "Authorization": f"Bearer {access_token}",
                "X-Goog-User-Project": project_id,
            },
            max_size=_MAX_MESSAGE_BYTES,
            open_timeout=_OPEN_TIMEOUT_S,
        )
        logger.info(
            "Connected to CES with session ID: %s",
            self._session_name,
            extra=self._log_extra("ces_connect", None),
        )

    async def _send_setup(
        self, start: base.SessionStart, rehydration_payload: str | None
    ) -> None:
        config = {
            "session": self._session_name,
            "inputAudioConfig": {
                "audioEncoding": "MULAW",
                "sampleRateHertz": 8000,
            },
            "outputAudioConfig": {
                "audioEncoding": "MULAW",
                "sampleRateHertz": 8000,
            },
        }
        if start.deployment_id:
            config["deployment"] = start.deployment_id
        if rehydration_payload:
            config["historicalContexts"] = [
                {
                    "role": "user",
                    "chunks": [
                        {"text": f"[SESSION_CONTEXT]{rehydration_payload}"}
                    ],
                }
            ]
        config_message = {"config": config}
        await self._send_json(
            config_message,
            "Error sending config message to CES",
            "ces_send_config_error",
        )
        logger.info(
            "Sent config message to CES",
            extra=self._log_extra(
                "ces_send_config", {"data": redaction.redact(config_message)}
            ),
        )

        if start.variables:
            variables_message = {
                "realtimeInput": {"variables": dict(start.variables)}
            }
            await self._send_json(
                variables_message,
                "Error sending variables to CES",
                "ces_send_variables_error",
            )
            logger.info(
                "Sent variables to CES",
                extra=self._log_extra(
                    "ces_send_variables",
                    {"data": redaction.redact(variables_message)},
                ),
            )

        if start.initial_message:
            kickstart = {"realtimeInput": {"text": start.initial_message}}
            message, log_type = (
                "Sent kickstart message (text) to CES",
                "ces_send_kickstart_text",
            )
        else:
            kickstart = {"realtimeInput": {"event": {"event": "session_start"}}}
            message, log_type = (
                "Sent session_start event to CES",
                "ces_send_session_start",
            )
        await self._send_json(
            kickstart,
            "Error sending kickstart/event message to CES",
            "ces_send_kickstart_error",
        )
        logger.info(
            message, extra=self._log_extra(log_type, {"data": kickstart})
        )

    async def _send_json(
        self,
        message: Mapping[str, Any],
        error_text: str,
        error_log_type: str,
    ) -> None:
        try:
            await self._websocket.send(json.dumps(message))
        except Exception:
            logger.error(
                error_text,
                exc_info=True,
                extra=self._log_extra(error_log_type, None),
            )
            raise

    async def send_audio(self, chunk: bytes) -> None:
        """Sends caller audio; dropped silently if not connected.

        Args:
          chunk: PCMU audio at 8 kHz.

        Raises:
          BackendSendError: The send failed.
        """
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "CESWS: send_audio: Received MULAW audio",
                extra=self._log_extra(
                    "ces_send_audio_recv", {"audio_size": len(chunk)}
                ),
            )
        if not self.is_connected():
            return
        payload = base64.b64encode(chunk).decode("utf-8")
        try:
            await self._websocket.send(
                json.dumps({"realtimeInput": {"audio": payload}})
            )
        except Exception as err:
            logger.error(
                "Error sending audio to CES",
                exc_info=True,
                extra=self._log_extra("ces_send_audio_error", None),
            )
            raise base.BackendSendError(str(err)) from err

    async def send_dtmf(self, digit: str) -> None:
        """Sends one DTMF digit.

        Args:
          digit: The digit, as received from Genesys.

        Raises:
          BackendSendError: The send failed for a reason other than the
            connection having closed.
        """
        redacted_digit = redaction.redact_value(digit)
        logger.info(
            "Attempting to send DTMF",
            extra=self._log_extra("ces_send_dtmf", {"digit": redacted_digit}),
        )
        connected = self.is_connected()
        logger.info(
            "CES WS connected state",
            extra=self._log_extra("ces_send_dtmf", {"connected": connected}),
        )
        if not connected:
            logger.warning(
                "Cannot send DTMF, CES WS not connected",
                extra=self._log_extra(
                    "ces_send_dtmf_error", {"digit": redacted_digit}
                ),
            )
            return
        try:
            await self._websocket.send(
                json.dumps({"realtimeInput": {"dtmf": digit}})
            )
        except websockets.exceptions.ConnectionClosedError as err:
            logger.warning(
                "Failed to send DTMF, CES connection closed",
                extra=self._log_extra(
                    "ces_send_dtmf_closed",
                    {"digit": redacted_digit, "error": str(err)},
                ),
            )
            return
        except Exception as err:
            logger.error(
                "Error sending DTMF to CES",
                exc_info=True,
                extra=self._log_extra(
                    "ces_send_dtmf_error",
                    {"digit": redacted_digit, "error": str(err)},
                ),
            )
            raise _dtmf_send_error(redacted_digit, err) from err
        logger.info(
            "Sent DTMF to CES",
            extra=self._log_extra("ces_send_dtmf", {"digit": redacted_digit}),
        )

    async def send_event(self, name: str) -> None:
        """Sends a named event; failures are logged, not raised.

        Args:
          name: The event name.
        """
        logger.info(
            "Attempting to send '%s' event to CES",
            name,
            extra=self._log_extra("ces_send_event", None),
        )
        if not self.is_connected():
            logger.warning(
                "Cannot send event, CES WS not connected",
                extra=self._log_extra("ces_send_event_skip", None),
            )
            return
        event_message = {"realtimeInput": {"event": {"event": name}}}
        try:
            await self._websocket.send(json.dumps(event_message))
        except Exception:  # pylint: disable=broad-exception-caught
            logger.error(
                "Error sending '%s' event to CES",
                name,
                exc_info=True,
                extra=self._log_extra("ces_send_event_error", None),
            )
            return
        logger.info(
            "Sent '%s' event to CES",
            name,
            extra=self._log_extra("ces_send_event_success", event_message),
        )

    async def send_half_close(self) -> None:
        """Sends the `clientHalfClose` frame.

        CES ignores this frame apart from resetting its idle timer; closing
        the socket is the real half-close. It is kept until validation run
        V1 confirms it can be removed.
        """
        if not self.is_connected():
            return
        logger.info(
            "Sending clientHalfClose to CES",
            extra=self._log_extra("ces_send_halfclose", None),
        )
        try:
            await self._websocket.send(json.dumps({"clientHalfClose": True}))
        except Exception as err:  # pylint: disable=broad-exception-caught
            logger.error(
                "Error sending clientHalfClose: %s",
                err,
                extra=self._log_extra("ces_halfclose_error", None),
            )

    async def receive(self) -> base.BackendEvent | None:
        """Waits for the next CES message and translates it.

        Returns:
          The backend event, or None for messages that need no action.
        """
        try:
            message = await self._websocket.recv()
            return self._translate(json.loads(message))
        except websockets.exceptions.ConnectionClosed as err:
            close_frame = err.rcvd
            code = close_frame.code if close_frame else _ABNORMAL_CLOSE_CODE
            reason = close_frame.reason if close_frame else ""
            return base.StreamEnded(
                code=code,
                reason=reason,
                detail=str(err),
                clean=code in _CLEAN_CLOSE_CODES,
                sys_error=close_sys_error(code, reason),
            )
        except Exception as err:  # pylint: disable=broad-exception-caught
            logger.error(
                "Error in CES listener",
                exc_info=True,
                extra=self._log_extra("ces_listener_error", None),
            )
            return base.BackendError(f"CES Listen Error: {err}")

    def _translate(self, data: Mapping[str, Any]) -> base.BackendEvent | None:
        session_output = data.get("sessionOutput")
        if "interruptionSignal" in data:
            logger.info(
                "Received InterruptionSignal from CES",
                extra=self._log_extra("ces_recv_interruption", None),
            )
            return base.BargeIn()
        if session_output is not None and "audio" in session_output:
            audio = base64.b64decode(session_output["audio"])
            if logger.isEnabledFor(logging.DEBUG):
                logger.debug(
                    "CESWS: listen: Received MULAW audio",
                    extra=self._log_extra(
                        "ces_recv_audio", {"audio_size": len(audio)}
                    ),
                )
            return base.AudioOut(audio)
        if session_output is not None and "text" in session_output:
            text = session_output["text"]
            logger.info(
                "Received text from CES",
                extra=self._log_extra(
                    "ces_recv_text", {"text": redaction.redact(text)}
                ),
            )
            return base.AgentText(text)
        if "endSession" in data:
            logger.info(
                "Received endSession from CES",
                extra=self._log_extra("ces_recv_endsession", {"data": data}),
            )
            metadata = data["endSession"].get("metadata", {})
            return base.EndOfInteraction(metadata.get("params") or {})
        if "goAway" in data:
            logger.info(
                "Received goAway from CES (approaching session limit)",
                extra=self._log_extra("ces_recv_goaway", {"data": data}),
            )
            return base.Rollover("goAway")
        if "recognitionResult" in data:
            return None
        if session_output is not None:
            # DEBUG: these can carry large diagnosticInfo payloads (TC-4.4).
            logger.debug(
                "Received sessionOutput from CES",
                extra=self._log_extra(
                    "ces_recv_sessionoutput", {"data": redaction.redact(data)}
                ),
            )
            return None
        logger.warning(
            "Received unhandled message from CES",
            extra=self._log_extra(
                "ces_recv_unhandled", {"data": redaction.redact(data)}
            ),
        )
        return None

    async def close(self) -> None:
        """Closes the WebSocket; safe to call more than once."""
        if self.is_connected():
            logger.info(
                "Closing WebSocket connection to CES",
                extra=self._log_extra("ces_close", None),
            )
            await self._websocket.close()
        else:
            logger.info(
                "WebSocket connection to CES was already closed",
                extra=self._log_extra("ces_close", None),
            )


def _dtmf_send_error(
    redacted_digit: Any, err: Exception
) -> base.BackendSendError:
    """Classifies a DTMF send failure for the Genesys error report."""
    details = {"digit": redacted_digit, "originalError": str(err)}
    error_type = "DTMF_FAILURE"
    if "INVALID_ARGUMENT" in str(err) or "Invalid value" in str(err):
        error_type = "API_INVALID_ARGUMENT"
        details["violatedField"] = "realtime_input.dtmf"
    elif "DEADLINE_EXCEEDED" in str(err):
        error_type = "API_DEADLINE_EXCEEDED"
    return base.BackendSendError(
        "Failed to send DTMF to CES.", error_type=error_type, details=details
    )
