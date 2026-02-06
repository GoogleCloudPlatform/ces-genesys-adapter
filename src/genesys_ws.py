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
from .redaction import redact, redact_value
from websockets.protocol import State
from .config import LOG_UNREDACTED_DATA

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
        self.disconnect_initiated = False
        self.is_probe = False

    def _get_log_extra(self, log_type: str, data: dict = None):
        extra = {
            "log_type": log_type,
            "genesys_session_id": self.client_session_id,
            "genesys_conv_id": self.conversation_id,
            "server_seq": self.last_server_sequence_number,
            "client_seq": self.last_client_sequence_number
        }
        if data:
            extra.update(data)
        return extra

    async def handle_connection(self):
        self.ces_ws = CESWS(self)

        try:
            async for message in self.websocket:
                if isinstance(message, str):
                    await self.handle_text_message(message)
                elif isinstance(message, bytes):
                    await self.handle_binary_message(message)
        except websockets.exceptions.ConnectionClosedError as e:
            if self.disconnect_initiated:
                logger.info("Genesys WebSocket closed as expected after disconnect process started.", extra=self._get_log_extra(log_type="genesys_connection_closed"))
            else:
                logger.error("Genesys WebSocket closed unexpectedly mid-session.", extra=self._get_log_extra(log_type="genesys_error"), exc_info=True)
                if self.ces_ws and self.ces_ws.is_connected():
                    await self.send_disconnect("error", info=f"Genesys WS ConnectionClosedError: {e}")
        except Exception as e:
            logger.error("Error in Genesys WebSocket handler", exc_info=True, extra=self._get_log_extra(log_type="genesys_error"))
            if not self.disconnect_initiated:
                 await self.send_disconnect("error", info=f"WebSocket Error: {e}")
        finally:
            logger.info("Genesys connection loop finished. Cleaning up CES connection.", extra=self._get_log_extra(log_type="genesys_connection_cleanup"))
            if self.ces_ws:
                await self.ces_ws.close()

    async def handle_text_message(self, message):
        redacted_message = redact(message)
        logger.info("Received text message from Genesys", extra=self._get_log_extra(log_type="genesys_recv", data={"data": redacted_message}))
        try:
            data = json.loads(message)
            message_type = data.get('type')
            logger.info("Received Genesys message", extra=self._get_log_extra(log_type="genesys_recv_parsed", data={"message_type": message_type}))
            self.last_client_sequence_number = data.get("seq")
            self.client_session_id = data.get("id")
            message_type = data.get("type")

            if message_type == "open":
                parameters = data.get("parameters", {})
                self.conversation_id = parameters.get("conversationId")

                if self.conversation_id == "00000000-0000-0000-0000-000000000000":
                    self.is_probe = True
                    logger.info("Connection Probe detected (Null UUID). Skipping CES connection.", extra=self._get_log_extra(log_type="genesys_probe"))

                self.input_variables = parameters.get("inputVariables")

                self.deployment_id = None
                self.agent_id = None

                if not self.is_probe:
                    self.initial_message = None
                    self.session_id = None
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
                                logger.error("Invalid _deployment_id format", extra=self._get_log_extra(log_type="genesys_config_error", data={"deployment_id": self.deployment_id}))
                                await self.send_disconnect(
                                    "error", "Invalid _deployment_id format"
                                )
                                return
                        elif "_agent_id" in self.input_variables:
                            self.agent_id = self.input_variables["_agent_id"]
                        if "_initial_message" in self.input_variables:
                            self.initial_message = self.input_variables["_initial_message"]
                        if "_session_id" in self.input_variables:
                            self.session_id = self.input_variables["_session_id"]

                        self.ces_input_variables = {
                            k: v
                            for k, v in self.input_variables.items()
                            if not k.startswith("_")
                        }

                    if not self.agent_id:
                        logger.error("Missing _deployment_id or _agent_id", extra=self._get_log_extra(log_type="genesys_config_error", data={"input_variables": self.input_variables}))
                        await self.send_disconnect(
                            "error",
                            "Missing required parameter: _agent_id or _deployment_id",
                        )
                        return

                    if not await self.ces_ws.connect(self.agent_id, self.deployment_id, self.session_id):
                        logger.error("CES connection failed, stopping setup", extra=self._get_log_extra(log_type="genesys_config_error"))
                        return # Disconnect is handled within ces_ws.connect

                    try:
                        asyncio.create_task(self.ces_ws.listen())
                        asyncio.create_task(self.ces_ws.pacer())
                    except Exception as e:
                        logger.error("Error creating CES listener or pacer tasks", exc_info=True, extra=self._get_log_extra(log_type="genesys_ces_task_error"))
                        await self.send_disconnect("error", f"Task creation Error: {e}")
                        return

                    logger.info("Genesys session opened", extra=self._get_log_extra(log_type="genesys_open"))

                custom_config_str = parameters.get("customConfig")
                if custom_config_str:
                    logger.info("Found customConfig from Genesys", extra=self._get_log_extra(log_type="genesys_custom_config", data={"custom_config_str": custom_config_str}))
                    try:
                        custom_config = json.loads(custom_config_str)
                        if isinstance(custom_config, dict):
                            for key, value in custom_config.items():
                                logger.info("Custom config item", extra=self._get_log_extra(log_type="genesys_custom_config", data={"key": key, "value": value}))
                        else:
                            logger.warning("Custom config is not a dict", extra=self._get_log_extra(log_type="genesys_custom_config", data={"custom_config": custom_config}))
                    except json.JSONDecodeError:
                        logger.error("Error decoding customConfig JSON", extra=self._get_log_extra(log_type="genesys_custom_config_error", data={"custom_config": custom_config_str}))

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
                    "clientseq": self.last_client_sequence_number,
                    "parameters": {"startPaused": False, "media": [selected_media]},
                }
                logger.info("Sending 'opened' message to Genesys", extra=self._get_log_extra(log_type="genesys_send_opened", data={"opened_msg": opened_message}))
                await self.send_message(opened_message)

            elif message_type == "ping":
                pong_message = {
                    "type": "pong",
                    "version": "2",
                    "id": self.client_session_id,
                    "clientseq": self.last_client_sequence_number,
                }
                await self.send_message(pong_message)

            elif message_type == "playback_started":
                logger.info("Received playback-started from Genesys", extra=self._get_log_extra(log_type="genesys_recv_playback_started", data={"data": data}))

            elif message_type == "playback_completed":
                logger.info("Received playback-completed from Genesys", extra=self._get_log_extra(log_type="genesys_recv_playback_completed", data={"data": data}))

            elif message_type == "dtmf":
                digit = data.get("parameters", {}).get("digit")
                if digit:
                    logger.info("Received DTMF from Genesys", extra=self._get_log_extra(log_type="genesys_recv_dtmf", data={"digit": redact_value(digit)}))
                    if self.ces_ws:
                        await self.ces_ws.send_dtmf(digit)
                else:
                    logger.warning("Received DTMF message without digit", extra=self._get_log_extra(log_type="genesys_recv_dtmf_missing", data={"data": data}))

            elif message_type == "close":
                closed_message = {
                    "type": "closed",
                    "version": "2",
                    "id": self.client_session_id,
                    "clientseq": self.last_client_sequence_number,
                    "parameters": {}
                }
                logger.info("Sending closed message to Genesys", extra=self._get_log_extra(log_type="genesys_send_closed", data={"closed_message": closed_message}))
                await self.send_message(closed_message)
                # We specifically DO NOT close the WebSocket here.
                # AudioHook requires Genesys to receive this message before the TCP connection drops.
                # We return to the read loop and let Genesys initiate the WebSocket CLOSE handshake.

            elif message_type == "update":
                logger.info("Received update message from Genesys", extra=self._get_log_extra(log_type="genesys_recv_update", data={"data": data}))

            else:
                logger.info("Received unhandled message type from Genesys", extra=self._get_log_extra(log_type="genesys_recv_unhandled", data={"message_type": message_type, "data": data}))

        except json.JSONDecodeError:
            logger.error("Error decoding JSON from Genesys", extra=self._get_log_extra(log_type="genesys_json_decode_error", data={"message": message}))
            await self.send_disconnect("error", "Invalid JSON received")

    async def send_disconnect(self, reason="normal", info=None, output_variables=None):
        if self.disconnect_initiated:
            logger.info("Disconnect already in progress, skipping duplicate call", extra=self._get_log_extra(log_type="genesys_disconnect_duplicate"))
            return
        self.disconnect_initiated = True
        logger.info("Preparing to send disconnect", extra=self._get_log_extra(log_type="genesys_disconnect_start", data={"reason": reason, "info": info, "output_variables": output_variables}))
        disconnect_message = {
            "type": "disconnect",
            "version": "2",
            "id": self.client_session_id,
            "clientseq": self.last_client_sequence_number,
            "parameters": {"reason": reason},
        }

        if info:
            disconnect_message["parameters"]["info"] = info
        if output_variables:
            disconnect_message["parameters"]["outputVariables"] = {
                k: v if isinstance(v, str) else json.dumps(v, default=str)
                for k, v in output_variables.items()
            }

        if self.ces_ws:
            logger.info("Stopping audio and clearing queues...", extra=self._get_log_extra(log_type="genesys_disconnect_audio_drain"))
            await self.ces_ws.stop_audio()
            logger.info("Audio queues cleared by stop_audio.", extra=self._get_log_extra(log_type="genesys_disconnect_audio_drain"))

        logger.info("Sending disconnect message to Genesys", extra=self._get_log_extra(log_type="genesys_send_disconnect", data={"disconnect_message": redact(disconnect_message)}))
        await self.send_message(disconnect_message)


    def get_next_server_sequence_number(self):
        self.last_server_sequence_number += 1
        return self.last_server_sequence_number

    async def handle_binary_message(self, message):
        if self.disconnect_initiated:
            logger.info("GenesysWS: Ignoring binary message during disconnect", extra=self._get_log_extra(log_type="genesys_ignore_binary"))
            return

        logger.info("GenesysWS: Received binary message", extra=self._get_log_extra(log_type="genesys_recv_binary", data={"audio_size": len(message)}))
        if self.ces_ws:
            await self.ces_ws.send_audio(message)

    async def send_message(self, message):
        if not self.websocket or self.websocket.state != State.OPEN:
            logger.warning("Attempted to send message on non-open WebSocket", extra=self._get_log_extra(log_type="genesys_send_error", data={"payload": redact(message)}))
            return
        try:
            message['seq'] = self.get_next_server_sequence_number()
            logger.debug("Sending message to Genesys", extra=self._get_log_extra(log_type="genesys_send", data={"payload": redact(message)}))
            await self.websocket.send(json.dumps(message))
        except Exception as e:
            logger.error("Error sending message to Genesys", exc_info=True, extra=self._get_log_extra(log_type="genesys_send_error", data={"payload": redact(message)}))
            raise

    async def send_error_report(self, errorType, errorMessage, source=None, details=None):
        error_payload = {
            "type": "errorReport",
            "payload": {
                "errorType": errorType,
                "errorMessage": errorMessage
            }
        }
        if source:
            error_payload["payload"]["source"] = source
        if details:
            error_payload["payload"]["details"] = details

        data_message = {
            "type": "data",
            "payload": error_payload
        }
        try:
            await self.send_message(data_message)
            logger.info("Sent error report to Genesys", extra=self._get_log_extra(log_type="genesys_send_error_report", data={
                "errorType": errorType,
                "errorMessage": errorMessage,
                "details": details
            }))
        except Exception as e:
            logger.error("Failed to send error report to Genesys", exc_info=True, extra=self._get_log_extra(log_type="genesys_error_report_failed", data={
                "errorType": errorType
            }))
        except Exception as e:
            logger.error("Failed to send error report to Genesys", exc_info=True, extra=self._get_log_extra(log_type="genesys_error_report_failed", data={
                "errorType": errorType
            }))

            logger.error("Failed to send error report to Genesys", exc_info=True, extra=self._get_log_extra({
                "errorType": errorType
            }))
