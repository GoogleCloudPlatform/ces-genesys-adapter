# Changelog

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
