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

"""Contract between a call session and a virtual-agent backend.

A backend owns one transport to a virtual agent (for example the CES
BidiRunSession WebSocket). It turns transport messages into the backend
events defined here, so the call session never handles JSON, protos or
transport exceptions.
"""

from collections.abc import Callable, Mapping
import dataclasses
from typing import Any, Protocol

# Builds the structured-logging `extra` dict for a log line.
LogExtra = Callable[[str, Mapping[str, Any] | None], dict[str, Any]]


@dataclasses.dataclass(frozen=True)
class SessionStart:
    """Everything a backend needs to open a session for one call.

    Attributes:
      agent_id: Full agent resource name, `projects/.../apps/APP_ID`.
      deployment_id: Full deployment resource name, or None.
      session_id: Session ID chosen by the call session's ID policy.
      variables: Agent input variables sent when the session starts.
      initial_message: Text kickstart, or None to send `session_start`.
    """

    agent_id: str
    deployment_id: str | None
    session_id: str
    variables: Mapping[str, Any]
    initial_message: Any | None = None


@dataclasses.dataclass(frozen=True)
class AudioOut:
    """Agent audio to play to the caller (PCMU, 8 kHz)."""

    audio: bytes


@dataclasses.dataclass(frozen=True)
class AgentText:
    """Agent text output (already logged by the backend)."""

    text: str


@dataclasses.dataclass(frozen=True)
class BargeIn:
    """The caller interrupted the agent; queued agent audio is stale."""


@dataclasses.dataclass(frozen=True)
class EndOfInteraction:
    """The agent ended the session.

    Attributes:
      output_variables: Variables to return to Genesys.
      kind: `end`, `handoff` or `transfer`.
    """

    output_variables: Mapping[str, Any]
    kind: str = "end"


@dataclasses.dataclass(frozen=True)
class Rollover:
    """The backend asked the client to move to a new stream."""

    reason: str


@dataclasses.dataclass(frozen=True)
class StreamEnded:
    """The backend stream closed.

    Attributes:
      code: Transport close code (WebSocket close code for CES).
      reason: Close reason text, possibly empty.
      detail: Human-readable description of the close.
      clean: Whether the close code is a normal closure.
      sys_error: `SYS_ERROR` output-variable value for this close, if the
        close maps to the graceful Architect error path.
    """

    code: int
    reason: str
    detail: str
    clean: bool
    sys_error: str | None = None


@dataclasses.dataclass(frozen=True)
class BackendError:
    """An unexpected error while reading from the backend."""

    message: str


BackendEvent = (
    AudioOut
    | AgentText
    | BargeIn
    | EndOfInteraction
    | Rollover
    | StreamEnded
    | BackendError
)


class BackendOpenError(Exception):
    """The backend could not open or configure a session.

    Attributes:
      retryable: Whether the failure is transient (timeout, 5xx,
        UNAVAILABLE, DEADLINE_EXCEEDED) and worth retrying.
    """

    def __init__(self, message: str, retryable: bool = False):
        """Initializes the error.

        Args:
          message: Description of the failure.
          retryable: Whether the failure is transient.
        """
        super().__init__(message)
        self.retryable = retryable


class BackendSendError(Exception):
    """Sending to the backend failed.

    Attributes:
      error_type: Short error category for Genesys error reports.
      details: Extra details for Genesys error reports.
    """

    def __init__(
        self,
        message: str,
        error_type: str = "SEND_FAILURE",
        details: Mapping[str, Any] | None = None,
    ):
        """Initializes the error.

        Args:
          message: Description of the failure.
          error_type: Short error category for Genesys error reports.
          details: Extra details for Genesys error reports.
        """
        super().__init__(message)
        self.error_type = error_type
        self.details = dict(details or {})


class VirtualAgentBackend(Protocol):
    """Transport to a virtual agent for one call."""

    mode: str

    @property
    def agent_session_id(self) -> str | None:
        """The backend's session or conversation name, once known."""

    def is_connected(self) -> bool:
        """Returns whether the transport is open."""

    async def open(
        self, start: SessionStart, *, rehydration_payload: str | None = None
    ) -> None:
        """Opens the transport and starts the session.

        Args:
          start: Session parameters.
          rehydration_payload: Context replayed when reconnecting after a
            rollover. To be removed in favor of a plain resume (D10).

        Raises:
          BackendOpenError: The transport or session setup failed.
        """

    async def send_audio(self, chunk: bytes) -> None:
        """Sends caller audio (PCMU, 8 kHz).

        Raises:
          BackendSendError: The send failed.
        """

    async def send_dtmf(self, digit: str) -> None:
        """Sends one DTMF digit.

        Raises:
          BackendSendError: The send failed for a reason worth reporting.
        """

    async def send_event(self, name: str) -> None:
        """Sends a named event; failures are logged, not raised."""

    async def send_half_close(self) -> None:
        """Signals that the client has no more input."""

    async def receive(self) -> BackendEvent | None:
        """Waits for the next transport message.

        Returns:
          The event for the message, or None if the message needs no action.
        """

    async def close(self) -> None:
        """Closes the transport; safe to call more than once."""
