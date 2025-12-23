# Copyright 2025 Google LLC

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     https://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import asyncio
import json
import logging

from .ces_ws import CESWS
from .redaction import redact

logger = logging.getLogger(__name__)


class GenesysWS:
    def __init__(self, websocket):
        self.websocket = websocket
        self.ces_ws = None
        self.last_server_sequence_number = 0
        self.last_client_sequence_number = 0
        self.client_session_id = None
        self.conversation_id = None
        self.input_variables = None

    async def handle_connection(self):
        self.ces_ws = CESWS(self)

        async for message in self.websocket:
            if isinstance(message, str):
                await self.handle_text_message(message)
            elif isinstance(message, bytes):
                await self.handle_binary_message(message)

    async def handle_text_message(self, message):
        redacted_message = redact(message)
        logger.info(f"Received text message from Genesys: {redacted_message}")
        try:
            data = json.loads(message)
            self.last_client_sequence_number = data.get("seq")
            self.client_session_id = data.get("id")
            message_type = data.get("type")

            if message_type == "open":
                parameters = data.get("parameters", {})
                self.conversation_id = parameters.get("conversationId")
                self.input_variables = parameters.get("inputVariables")

                self.deployment_id = None
                self.agent_id = None

                if self.input_variables:
                    if "_deployment_id" in self.input_variables:
                        self.deployment_id = self.input_variables["_deployment_id"]
                        # Extract agent_id from deployment_id
                        # deployment_id format:
                        # projects/{project}/locations/{location}/apps/{app_id}/deployments/{deployment_id}
                        # agent_id format:
                        # projects/{project}/locations/{location}/apps/{app_id}
                        parts = self.deployment_id.split("/")
                        if len(parts) == 8 and parts[6] == "deployments":
                            self.agent_id = "/".join(parts[:6])
                        else:
                            logger.error(
                                f"Invalid _deployment_id format: {self.deployment_id}"
                            )
                            await self.send_disconnect(
                                "error", "Invalid _deployment_id format"
                            )
                            return
                    elif "_agent_id" in self.input_variables:
                        self.agent_id = self.input_variables["_agent_id"]

                    self.ces_input_variables = {
                        k: v
                        for k, v in self.input_variables.items()
                        if not k.startswith("_")
                    }

                if not self.agent_id:
                    logger.error(
                        "'_agent_id' or '_deployment_id'"
                        "not found in inputVariables from Genesys."
                    )
                    await self.send_disconnect(
                        "error",
                        "Missing required parameter: _agent_id or _deployment_id",
                    )
                    return

                await self.ces_ws.connect(self.agent_id, self.deployment_id)
                asyncio.create_task(self.ces_ws.listen())
                asyncio.create_task(self.ces_ws.pacer())

                logger.info(
                    "Genesys session opened for conversation ID: "
                    f"{self.conversation_id}"
                )

                custom_config_str = parameters.get("customConfig")
                if custom_config_str:
                    logger.info("Found customConfig from Genesys:")
                    try:
                        custom_config = json.loads(custom_config_str)
                        if isinstance(custom_config, dict):
                            for key, value in custom_config.items():
                                logger.info(f"  - {key}: {value}")
                        else:
                            logger.warning("CUSTOMCONFIG IS NOT A KEY-VALUE PAIR:")
                            logger.warning(f"  {custom_config}")
                    except json.JSONDecodeError:
                        logger.error(
                            f"Error decoding customConfig JSON: {custom_config_str}"
                        )

                offered_media = parameters.get("media", [])
                selected_media = None
                for media_option in offered_media:
                    if (
                        media_option.get("type") == "audio"
                        and media_option.get("format") == "PCMU"
                        and media_option.get("rate") == 8000
                    ):
                        selected_media = media_option
                        break

                if not selected_media:
                    await self.send_disconnect(
                        "error", "No compatible audio media offered."
                    )
                    return

                opened_message = {
                    "type": "opened",
                    "version": "2",
                    "id": self.client_session_id,
                    "seq": self.get_next_server_sequence_number(),
                    "clientseq": self.last_client_sequence_number,
                    "parameters": {"startPaused": False, "media": [selected_media]},
                }
                await self.send_message(opened_message)

            elif message_type == "ping":
                pong_message = {
                    "type": "pong",
                    "version": "2",
                    "id": self.client_session_id,
                    "seq": self.get_next_server_sequence_number(),
                    "clientseq": self.last_client_sequence_number,
                }
                await self.send_message(pong_message)

            elif message_type == "close":
                closed_message = {
                    "type": "closed",
                    "id": self.client_session_id,
                    "seq": self.get_next_server_sequence_number(),
                    "clientseq": self.last_client_sequence_number,
                }
                await self.send_message(closed_message)
                await self.websocket.close()
                if self.ces_ws:
                    await self.ces_ws.close()

            elif message_type == "update":
                logger.info(f"Received update message from Genesys: {data}")
                pass

        except json.JSONDecodeError:
            logger.error(f"Error decoding JSON from Genesys: {message}")
            await self.send_disconnect("error", "Invalid JSON received")

    async def send_disconnect(self, reason, params=None):
        """
        Sends a 'disconnect' message to Genesys and closes the WebSocket
        connection.
        """
        logger.info(f"Sending disconnect message to Genesys with reason: {reason}")
        disconnect_message = {
            "type": "disconnect",
            "version": "2",
            "id": self.client_session_id,
            "seq": self.get_next_server_sequence_number(),
            "clientseq": self.last_client_sequence_number,
            "parameters": {"reason": reason},
        }

        if params:
            output_variables = {}
            # Handle old calls that passed a string as 'info'
            if isinstance(params, str):
                output_variables["info"] = params
            elif isinstance(params, dict):
                for key, value in params.items():
                    output_variables[key] = value
            disconnect_message["parameters"]["outputVariables"] = output_variables

        await self.send_message(disconnect_message)
        await self.websocket.close()
        if self.ces_ws and self.ces_ws.is_connected():
            await self.ces_ws.close()

    def get_next_server_sequence_number(self):
        self.last_server_sequence_number += 1
        return self.last_server_sequence_number

    async def handle_binary_message(self, message):
        if self.ces_ws:
            await self.ces_ws.send_audio(message)

    async def send_message(self, message):
        await self.websocket.send(json.dumps(message))
