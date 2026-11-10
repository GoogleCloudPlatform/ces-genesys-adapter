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

"""Creates the virtual-agent backend for a connectivity mode."""

from src.backends import base
from src.backends import ces_websocket

DEFAULT_MODE = "ces"
SUPPORTED_MODES = (DEFAULT_MODE,)


def create_backend(
    log_extra: base.LogExtra, mode: str = DEFAULT_MODE
) -> base.VirtualAgentBackend:
    """Returns a new backend for one call.

    Args:
      log_extra: Builds the `extra` dict for the backend's log lines.
      mode: Connectivity mode. Only `ces` exists until the Dialogflow
        backend lands.

    Returns:
      An unopened backend.

    Raises:
      ValueError: The mode is not supported.
    """
    if mode == "ces":
        return ces_websocket.CesWebSocketBackend(log_extra)
    raise ValueError(f"Unsupported connectivity mode: {mode!r}")
