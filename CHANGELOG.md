# Changelog

## 2026-01-15

### Optimization

1.  **Removed Audio Transcoding:**
    -   **Change:** Updated the CES `BidiRunSession` configuration in `ces_ws.py` to use `AUDIO_ENCODING_MULAW` at `8000 Hz` for both input and output audio.
    -   **Impact:** This aligns the audio format with Genesys AudioHook's native 8kHz MULAW. All `audioop` based transcoding (ulaw2lin, lin2ulaw, ratecv) has been removed from `ces_ws.py`, significantly reducing CPU load and potential latency. The adapter now directly passes audio between Genesys and CES.

### Security

1.  **Added Startup Secret Check:**
    -   **Change:** Added a check in `main.py` to ensure the `GENESYS_CLIENT_SECRET` environment variable is set on startup.
    -   **Impact:** Prevents the adapter from running without the necessary secret for request signature verification.

---

## 2026-01-14

### Bug Fixes

1.  **Fixed Audio Leakage after Session End:**
    -   **Issue:** Genesys audio continued to be sent to CES even after CES initiated an `endSession`, potentially causing confusion or errors in late-state processing.
    -   **Fix:** Updated `GenesysWS.handle_binary_message` to check if a disconnect has been initiated. If so, incoming binary (audio) messages are ignored and logged with `log_type="genesys_ignore_binary"`.
    -   **Impact:** Ensures correct session termination flow where audio from Genesys is stopped immediately upon CES session end.

2.  **Resolved Disconnect Hang on Audio Queue Drain & AttributeError:**
    -   **Issue:** The adapter failed to send the `disconnect` message to Genesys, most notably in scenarios with high audio traffic from Genesys. The root cause evolved, but the symptom was `GenesysWS.send_disconnect` not completing. An `AttributeError` for `pacer_task` also occurred.
    -   **Cause:** Initial issues included the `pacer` task not stopping cleanly and `self.pacer_task` not being initialized. The final issue was an unnecessary and blocking `await self.ces_ws.audio_out_queue.join()` call in `GenesysWS.send_disconnect`.
    -   **Fix:**
        -   Initialized `self.pacer_task = None` and `self.audio_in_queue` in `CESWS.__init__`.
        -   Modified `CESWS.stop_audio` to explicitly cancel the pacer task and clear both `audio_out_queue` and `audio_in_queue`.
        -   Updated `CESWS.pacer` to handle `asyncio.CancelledError` gracefully, use `asyncio.wait_for` for non-blocking gets, and ensure `task_done()` is called via a `finally` block.
        -   **Removed the `await self.ces_ws.audio_out_queue.join()` call from `GenesysWS.send_disconnect`.**
        -   Added `await self.close()` in `CESWS.listen` after handling `endSession` to close the WebSocket to CES promptly.
    -   **Impact:** Ensures reliable and prompt shutdown. The `disconnect` message is now consistently sent to Genesys, and the CES WebSocket is closed as expected.

---

## 2026-01-13

### Bug Fixes

1.  **Resolved Genesys Sequence Number Errors (AUDIOHOOK-0004 / AUDIOHOOK-0009):**
    -   **Issue:** Genesys reported "Invalid seq number" errors, particularly for the initial `opened` message, where `seq: 2` was sent instead of the expected `seq: 1`.
    -   **Cause:** The `opened_message` dictionary in `genesys_ws.py` was still manually incrementing the sequence number before being passed to the centralized `send_message` function, causing a double increment.
    -   **Fix:** Removed the manual `"seq": self.get_next_server_sequence_number()` line from the `opened_message` definition in `genesys_ws.py`'s `handle_text_message` method.
    -   **Impact:** Ensures the sequence number is incremented only once in the `send_message` function, just before sending, for all message types.

2.  **Handled Unhandled `sessionOutput` Message from CES:**
    -   **Issue:** Logs showed warnings for unhandled `sessionOutput` messages from CES that did not contain `audio` or `text`.
    -   **Fix:** Added an `elif "sessionOutput" in data:` block in `ces_ws.py`'s `listen` method to catch these messages and log their redacted content at the INFO level.
    -   **Impact:** Prevents warning logs for these messages and provides visibility into their content.

### Refactoring

1.  **Added `log_type` to Structured Logs:**
    -   **Change:** Modified the `_get_log_extra` method in both `genesys_ws.py` and `ces_ws.py` to accept a mandatory `log_type` string argument.
    -   Updated all logging calls using `_get_log_extra` in both files to include a specific `log_type` key.
    -   **Impact:** Allows for easier filtering and analysis of logs in Cloud Logging based on the functional purpose of the log entry.
    -   **User Info:** You can now filter logs in Cloud Logging using the `jsonPayload.log_type` field. Some example values include:
        -   `init`: Application initialization.
        -   `config`: Configuration loading and checks.
        -   `auth`: Authentication events for incoming connections.
        -   `auth_error`: Authentication failures.
        -   `connection_start`: New WebSocket connection established.
        -   `genesys_recv`: Raw message received from Genesys.
        -   `genesys_recv_parsed`: Parsed message from Genesys.
        -   `genesys_send`: Message sent to Genesys.
        -   `genesys_send_opened`: 'opened' message sent to Genesys.
        -   `genesys_open`: Genesys session open event.
        -   `genesys_disconnect_start`: Disconnect process initiated.
        -   `genesys_disconnect_duplicate`: Attempted duplicate disconnect.
        -   `genesys_send_disconnect`: 'disconnect' message sent to Genesys.
        -   `genesys_send_closed`: 'closed' message sent to Genesys.
        -   `genesys_probe`: Detected connection probe from Genesys.
        -   `genesys_ignore_binary`: Ignored binary message during disconnect.
        -   `genesys_recv_dtmf`: DTMF received from Genesys.
        -   `genesys_custom_config`: Custom config received from Genesys.
        -   `genesys_error`: General error in Genesys WebSocket handling.
        -   `ces_connect`: Connection attempt to CES.
        -   `ces_connect_error`: Error during CES connection.
        -   `ces_send_config`: Config message sent to CES.
        -   `ces_send_kickstart`: Kickstart message sent to CES.
        -   `ces_send_audio`: Audio sent to CES.
        -   `ces_send_audio_convert`: Audio conversion before sending to CES.
        -   `ces_send_dtmf`: DTMF sent to CES.
        -   `ces_recv_audio`: Audio received from CES.
        -   `ces_recv_audio_convert`: Audio conversion after receiving from CES.
        -   `ces_recv_text`: Text received from CES.
        -   `ces_recv_interruption`: Interruption signal received from CES.
        -   `ces_recv_endsession`: Session end event from CES.
        -   `ces_recv_sessionoutput`: Other sessionOutput from CES.
        -   `ces_pacer_start`: Audio pacer task started.
        -   `ces_pacer_stop`: Audio pacer task stopped.
        -   `ces_pacer_queue_recv`: Audio chunk received from queue in pacer.
        -   `ces_pacer_send`: Audio chunk sent to Genesys from pacer.
        -   `ces_pacer_batch_sent`: Pacer completed sending a batch.
        -   `ces_connection_closed`: CES WebSocket closed.
        -   `ces_listener_error`: Error in CES listener task.
        -   `ces_pacer_error`: Error in CES pacer task.
        -   `*_unhandled`: Unhandled message type from Genesys or CES.
        -   `*_error`: Indicates an error in the specified component (e.g., `genesys_send_error`, `ces_send_dtmf_error`).

### Internal Clean-up

-   Removed temporary verbose debugging logs from the `send_message` function in `genesys_ws.py` after confirming the sequence number fix.

---

## 2026-01-12

1.  **Barge-in Handling**: Added support for `InterruptionSignal` from CES to handle customer barge-ins, clearing the outbound audio queue. 
2.  **DTMF Support**: Implemented handling of DTMF messages from Genesys, forwarding digits to CES. 
3.  **Improved Audio Streaming**: Adjusted audio format and handling to better align with Genesys Audio Connector requirements. 
4.  **Audio Chunking**: Introduced audio chunking (32KB every 200ms) to prevent audio frame issues and improve stability with Genesys. 
5.  **Structured JSON Logging**: Implemented `logging_utils.py` for structured Cloud Logging, enriching logs with session IDs and other context. 
6.  **Robust Error Handling**: Added comprehensive `try...except` blocks in WebSocket send/receive loops and async task management in both `genesys_ws.py` and `ces_ws.py` to catch errors and trigger graceful disconnects. 
7.  **Graceful Shutdown**: Enhanced the `send_disconnect` logic to ensure the audio output queue is fully drained before closing WebSocket connections. 
8.  **Prevent Duplicate Disconnects**: Introduced a `disconnect_initiated` flag in `GenesysWS` to prevent multiple disconnect messages from being sent during a single closure event.
9.  **endSession Metadata Passthrough**: Ensured that the `params` from the CES `endSession` message are correctly passed as `outputVariables` in the disconnect message to Genesys. 
10. **AudioHook Protocol Fixes**: Addressed issues resulting in AUDIOHOOK-0004 and AUDIOHOOK-0009 errors in the end of session handling. 
11. **Dynamic Initial Message**: Added support for `_initial_message` in input variables, allowing the custom configuration of the conversation kickstart message (defaulting to "Hello").
