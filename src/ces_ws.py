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
import audioop
import base64
import json
import logging
import uuid

import google.auth
import websockets
from websockets.connection import State

from .auth import auth_provider
from .redaction import redact, redact_value

logger = logging.getLogger(__name__)

_BASE_WS_URL = (
    "wss://ces.googleapis.com/ws/google.cloud.ces.v1.SessionService/"
    "BidiRunSession/locations/"
)


class CESWS:
    def __init__(self, genesys_ws):
        self.genesys_ws = genesys_ws
        self.websocket = None
        self.session_id = None
        self.deployment_id = None
        self.ratecv_state_to_va = None
        self.ratecv_state_to_genesys = None
        self.audio_in_queue = asyncio.Queue() # Genesys to CES
        self.audio_out_queue = asyncio.Queue() # CES to Genesys
        self._stop_pacer_event = asyncio.Event()
        self.pacer_task = None

    def _get_log_extra(self, log_type: str, data: dict = None):
        extra = {
            "log_type": log_type,
            "ces_session_id": self.session_id,
        }
        if self.genesys_ws:
             # Pass a generic type to genesys_ws, the specific type is already in extra
             base_extra = self.genesys_ws._get_log_extra(log_type="ces_related")
             base_extra.update(extra) # Merge, CES log_type takes precedence
             extra = base_extra
        
        if data:
            extra.update(data)
        return extra

    def is_connected(self):
        return self.websocket and self.websocket.state == State.OPEN

    async def connect(self, agent_id, deployment_id=None, initial_message=None):
        self.session_id = f"{agent_id}/sessions/{uuid.uuid4()}"
        self.deployment_id = deployment_id
        self.initial_message = initial_message

        try:
            _, project_id = google.auth.default()

            try:
                parts = agent_id.split("/")
                location_index = parts.index("locations")
                location = parts[location_index + 1]
            except (ValueError, IndexError):
                logger.error("Could not extract location from agent_id", extra=self._get_log_extra(log_type="ces_connect_error", data={"agent_id": agent_id}))
                return False

            token = await auth_provider.get_token()
            ws_url = f"{_BASE_WS_URL}{location}"

            logger.info("Connecting to CES", extra=self._get_log_extra(log_type="ces_connect", data={"url": ws_url}))
            self.websocket = await websockets.connect(
                ws_url,
                additional_headers={
                    "Authorization": f"Bearer {token}",
                    "X-Goog-User-Project": project_id,
                },
                max_size=4 * 1024 * 1024  # Increase limit to 4MiB to prevent message size errors
            )
            logger.info("Connected to CES", extra=self._get_log_extra(log_type="ces_connect"))
            await self.send_config_message()
            return True
        except Exception as e:
            logger.error("Error during CES connect/config", exc_info=True, extra=self._get_log_extra(log_type="ces_connect_error"))
            if not self.genesys_ws.disconnect_initiated:
                await self.genesys_ws.send_disconnect("error", info=f"CES Connection/Config Error: {e}")
            return False

    async def send_config_message(self):
        config_message = {
            "config": {
                "session": self.session_id,
                "inputAudioConfig": {
                    "audioEncoding": "LINEAR16",
                    "sampleRateHertz": 16000,
                },
                "outputAudioConfig": {                    "audioEncoding": "LINEAR16",
                    "sampleRateHertz": 16000,
                },
            }
        }
        if self.deployment_id:
            config_message["config"]["deployment"] = self.deployment_id
        if self.genesys_ws.ces_input_variables:
            config_message["config"]["variables"] = self.genesys_ws.ces_input_variables
        try:
            await self.websocket.send(json.dumps(config_message))
        except Exception as e:
            logger.error("Error sending config message to CES", exc_info=True, extra=self._get_log_extra(log_type="ces_send_config_error"))
            raise
        logger.info("Sent config message to CES", extra=self._get_log_extra(log_type="ces_send_config", data={"data": redact(config_message)}))

        kickstart_text = self.initial_message if self.initial_message else "Hello"
        kickstart_message = {"realtimeInput": {"text": kickstart_text}}
        try:
            await self.websocket.send(json.dumps(kickstart_message))
        except Exception as e:
            logger.error("Error sending kickstart message to CES", exc_info=True, extra=self._get_log_extra(log_type="ces_send_kickstart_error"))
            raise
        logger.info("Sent kickstart message to CES", extra=self._get_log_extra(log_type="ces_send_kickstart", data={"data": kickstart_message}))

    async def send_audio(self, audio_chunk):
        linear_audio_8k = audioop.ulaw2lin(audio_chunk, 2)
        linear_audio_16k, self.ratecv_state_to_va = audioop.ratecv(
            linear_audio_8k, 2, 1, 8000, 16000, self.ratecv_state_to_va
        )
        logger.info("CESWS: send_audio: Converted to L16", extra=self._get_log_extra(log_type="ces_send_audio_convert", data={"audio_size": len(linear_audio_16k)}))
        base64_pcm_payload = base64.b64encode(linear_audio_16k).decode("utf-8")
        va_input = {"realtimeInput": {"audio": base64_pcm_payload}}
        if self.is_connected():
            try:
                await self.websocket.send(json.dumps(va_input))
            except Exception as e:
                logger.error("Error sending audio to CES", exc_info=True, extra=self._get_log_extra(log_type="ces_send_audio_error"))
                # Not re-raising here, as audio send failures are less critical than config messages
                await self.genesys_ws.send_disconnect("error", info=f"CES Send Audio Error: {e}")

    async def send_dtmf(self, digit): # Adding DTMF support
        logger.info("Attempting to send DTMF", extra=self._get_log_extra(log_type="ces_send_dtmf", data={"digit": redact_value(digit)}))
        dtmf_message = {"realtimeInput": {"dtmf": digit}}
        connected = self.is_connected()
        logger.info("CES WS connected state", extra=self._get_log_extra(log_type="ces_send_dtmf", data={"connected": connected}))
        if connected:
            try:
                await self.websocket.send(json.dumps(dtmf_message))
                logger.info("Sent DTMF to CES", extra=self._get_log_extra(log_type="ces_send_dtmf", data={"digit": redact_value(digit)}))
            except Exception as e:
                logger.error("Error sending DTMF to CES", exc_info=True, extra=self._get_log_extra(log_type="ces_send_dtmf_error", data={"digit": redact_value(digit), "error": str(e)}))
                error_type = "DTMF_FAILURE"
                error_details = {"digit": redact_value(digit), "originalError": str(e)}
                if "INVALID_ARGUMENT" in str(e) or "Invalid value" in str(e):
                    error_type = "API_INVALID_ARGUMENT"
                    error_details["violatedField"] = "realtime_input.dtmf"
                elif "DEADLINE_EXCEEDED" in str(e):
                    error_type = "API_DEADLINE_EXCEEDED"

                await self.genesys_ws.send_error_report(
                    errorType=error_type,
                    errorMessage="Failed to send DTMF to CES.",
                    source="CESWS.send_dtmf",
                    details=error_details
                )
        else:
            logger.warning("Cannot send DTMF, CES WS not connected", extra=self._get_log_extra(log_type="ces_send_dtmf_error", data={"digit": redact_value(digit)}))

    async def stop_audio(self):
        logger.info("Stopping audio pacer and clearing queues", extra=self._get_log_extra(log_type="ces_pacer_stop"))
        self._stop_pacer_event.set()
        if self.pacer_task:
            self.pacer_task.cancel()
            try:
                await self.pacer_task
            except asyncio.CancelledError:
                logger.info("Pacer task cancelled as expected", extra=self._get_log_extra(log_type="ces_pacer_stop"))
            except Exception as e:
                logger.error("Error during pacer task cancellation", extra=self._get_log_extra(log_type="ces_pacer_error"), exc_info=True)
            self.pacer_task = None

        # Clear any remaining items in the OUTBOUND queue (CES to Genesys)
        while not self.audio_out_queue.empty():
            try:
                self.audio_out_queue.get_nowait()
                self.audio_out_queue.task_done()
            except asyncio.QueueEmpty:
                break
            except ValueError:
                break
        logger.info("Audio OUTBOUND queue cleared", extra=self._get_log_extra(log_type="ces_pacer_stop"))

        # Clear any remaining items in the INBOUND queue (Genesys to CES)
        while not self.audio_in_queue.empty():
            try:
                self.audio_in_queue.get_nowait()
                self.audio_in_queue.task_done()  # Call task_done for consistency
            except asyncio.QueueEmpty:
                break
            except ValueError:
                 # Should not happen if task_done is only called here for this queue
                 pass
        logger.info("Audio INBOUND queue cleared", extra=self._get_log_extra(log_type="ces_inbound_queue_clear"))

    async def listen(self):
        while self.is_connected():
            try:
                message = await self.websocket.recv()
                data = json.loads(message)

                if "interruptionSignal" in data:
                    logger.info("Received InterruptionSignal from CES", extra=self._get_log_extra(log_type="ces_recv_interruption"))
                    # Clear the audio out queue
                    while not self.audio_out_queue.empty():
                        try:
                            self.audio_out_queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                    logger.info("Cleared audio output queue due to InterruptionSignal", extra=self._get_log_extra(log_type="ces_recv_interruption"))

                elif "sessionOutput" in data and "audio" in data["sessionOutput"]:
                    linear_audio_16k = base64.b64decode(data["sessionOutput"]["audio"])
                    logger.info("CESWS: listen: Received L16 audio", extra=self._get_log_extra(log_type="ces_recv_audio", data={"audio_size": len(linear_audio_16k), "channels": 1}))
                    linear_audio_8k, self.ratecv_state_to_genesys = audioop.ratecv(
                        linear_audio_16k,
                        2,
                        1,
                        16000,
                        8000,
                        self.ratecv_state_to_genesys,
                    )
                    mulaw_audio = audioop.lin2ulaw(linear_audio_8k, 2)
                    logger.info("CESWS: listen: Converted to mulaw", extra=self._get_log_extra(log_type="ces_recv_audio_convert", data={"audio_size": len(mulaw_audio), "channels": 1}))
                    await self.audio_out_queue.put(mulaw_audio)

                elif "sessionOutput" in data and "text" in data["sessionOutput"]:
                    text = data['sessionOutput']['text']
                    redacted_text = redact(text)
                    logger.info("Received text from CES", extra=self._get_log_extra(log_type="ces_recv_text", data={"text": redacted_text}))

                elif "endSession" in data:
                    logger.info("Received endSession from CES", extra=self._get_log_extra(log_type="ces_recv_endsession", data={"data": data}))
                    metadata = data.get("endSession", {}).get("metadata", {})
                    params = metadata.get("params")
                    if not self.genesys_ws.disconnect_initiated:
                        asyncio.create_task(self.genesys_ws.send_disconnect("completed", info="Session has ended successfully in CES", output_variables=params))
                    await self.close() # Close CES connection
                    break # Exit listener loop

                elif "recognitionResult" in data:
                    pass

                elif "sessionOutput" in data:
                    logger.info("Received sessionOutput from CES", extra=self._get_log_extra(log_type="ces_recv_sessionoutput", data={"data": redact(data)}))

                else:
                    logger.warning("Received unhandled message from CES", extra=self._get_log_extra(log_type="ces_recv_unhandled", data={"data": redact(data)}))
            except websockets.exceptions.ConnectionClosed as e:
                logger.info("CES WS connection closed", extra=self._get_log_extra(log_type="ces_connection_closed", data={"code": e.code, "reason": e.reason}))
                if not self.genesys_ws.disconnect_initiated:
                     await self.genesys_ws.send_disconnect("error", info=f"CES WS Closed: {e.code}")
                break
            except Exception as e:
                logger.error("Error in CES listener", extra=self._get_log_extra(log_type="ces_listener_error"), exc_info=True)
                if not self.genesys_ws.disconnect_initiated:
                    await self.genesys_ws.send_disconnect("error", info=f"CES Listen Error: {e}")
                break

    async def pacer(self):
        logger.info("Starting audio pacer for Genesys", extra=self._get_log_extra(log_type="ces_pacer_start"))
        MAX_GENESYS_CHUNK_SIZE = 32000  # Bytes
        MIN_INTERVAL = 0.2  # Seconds (200ms)
        try:
            while not self._stop_pacer_event.is_set():
                audio_chunk = None # Initialize audio_chunk
                try:
                    audio_chunk = await asyncio.wait_for(self.audio_out_queue.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue # Check stop event

                try: # NEW try block for task_done
                    logger.info("CESWS: pacer: Received from queue", extra=self._get_log_extra(log_type="ces_pacer_queue_recv", data={"audio_size": len(audio_chunk)}))

                    if not self.genesys_ws.websocket or self.genesys_ws.websocket.state == self.websocket.protocol.state.CLOSED:
                        logger.warning("Genesys WS closed, discarding audio chunk", extra=self._get_log_extra(log_type="ces_pacer_discard"))
                        continue

                    start_time = asyncio.get_event_loop().time()
                    offset = 0
                    try:
                        while offset < len(audio_chunk):
                            if self._stop_pacer_event.is_set():
                                logger.info("Pacer stop event set mid-chunk", extra=self._get_log_extra(log_type="ces_pacer_stop_midchunk"))
                                break
                            
                            end = min(offset + MAX_GENESYS_CHUNK_SIZE, len(audio_chunk))
                            chunk_to_send = audio_chunk[offset:end]
                            # logger.info("CESWS: pacer: Sending to binary Genesys", extra=self._get_log_extra(log_type="ces_pacer_send", data={"audio_size": len(chunk_to_send)}))
                            await self.genesys_ws.websocket.send(chunk_to_send)
                            offset = end
                    except websockets.exceptions.ConnectionClosed:
                        logger.warning("Genesys WS closed during send", extra=self._get_log_extra(log_type="ces_pacer_send_error"))
                        break  # Exit main while loop
                    except Exception as e:
                         logger.error("Error sending audio to Genesys", extra=self._get_log_extra(log_type="ces_pacer_send_error"), exc_info=True)
                         break # Exit main while loop
                    
                    if self._stop_pacer_event.is_set():
                        continue
                    
                    if offset < len(audio_chunk): # Connection was closed mid-chunk or other error
                        continue

                    send_duration = asyncio.get_event_loop().time() - start_time
                    sleep_duration = max(0, MIN_INTERVAL - send_duration)
                    # logger.info("Pacer sent chunk batch", extra=self._get_log_extra(log_type="ces_pacer_batch_sent", data={"send_duration": send_duration, "sleep_duration": sleep_duration}))
                    if not self._stop_pacer_event.is_set():
                        await asyncio.sleep(sleep_duration)
                finally: # NEW finally block
                    if audio_chunk:
                        try:
                            self.audio_out_queue.task_done()
                        except ValueError:
                            pass # Already done

        except asyncio.CancelledError:
            logger.info("Pacer task cancelled", extra=self._get_log_extra(log_type="ces_pacer_cancelled"))
            raise
        except websockets.exceptions.ConnectionClosed:
            logger.info("Genesys websocket connection closed, pacer stopped", extra=self._get_log_extra(log_type="ces_pacer_connection_closed"))
        except Exception as e:
            logger.error("Unexpected error in pacer", extra=self._get_log_extra(log_type="ces_pacer_error"), exc_info=True)
            if not self.genesys_ws.disconnect_initiated:
                 await self.genesys_ws.send_disconnect("error", info=f"Pacer Error: {e}")
        logger.info("Audio pacer for Genesys stopped", extra=self._get_log_extra(log_type="ces_pacer_stopped"))

    async def close(self):
        """Closes the WebSocket connection to CES."""
        if self.is_connected():
            logger.info("Closing WebSocket connection to CES", extra=self._get_log_extra(log_type="ces_close"))
            await self.websocket.close()
        else:
            logger.info("WebSocket connection to CES was already closed", extra=self._get_log_extra(log_type="ces_close"))
