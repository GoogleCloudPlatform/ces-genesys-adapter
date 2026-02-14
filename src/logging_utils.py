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

import logging
import json
from datetime import datetime
import re
from . import config

class JSONFormatter(logging.Formatter):
    """Custom JSON Formatter for Google Cloud Logging."""

    def format(self, record):
        log_entry = {
            "message": super().format(record),
            "severity": record.levelname,
            "timestamp": datetime.utcfromtimestamp(record.created).isoformat() + 'Z',
            "logger": record.name,
            "lineno": record.lineno,
            "pathname": record.pathname,
            "funcName": record.funcName,
        }

        # Custom parsing for websockets library logs
        if record.name.startswith("websockets"):
            # Render the message with arguments first
            rendered_msg = record.getMessage()
            msg = rendered_msg # Use the rendered message for parsing
            ws_trace = {}

            frame_regex = re.compile(r"^([<>])\s+(TEXT|BINARY)\s+(.+?)\s+\[(\d+)\s+bytes\]$")
            match = frame_regex.match(msg)

            if match:
                direction, frame_type, content, byte_length = match.groups()
                ws_trace['direction'] = "inbound" if direction == "<" else "outbound"
                ws_trace['frame_type'] = frame_type
                ws_trace['byte_length'] = int(byte_length)

                data_preview = content
                if frame_type == "TEXT":
                    data_preview = content.strip("'")

                if data_preview:
                    ws_trace['data_preview'] = data_preview[:50] + ("..." if len(data_preview) > 50 else "")
                    if frame_type == "TEXT":
                        try:
                            ws_trace['data_json'] = json.loads(data_preview)
                            ws_trace['data_json_status'] = "parsed"
                        except json.JSONDecodeError:
                            ws_trace['data_json_status'] = "decode_error"
                    else:
                        ws_trace['data_json_status'] = "not_attempted"
            elif msg.startswith("< "):
                ws_trace['direction'] = "inbound"
                header_part = msg[2:]
                if ":" in header_part:
                    try:
                        key, value = header_part.split(":", 1)
                        ws_trace['header'] = key.strip()
                        ws_trace['value'] = value.strip()
                    except ValueError:
                        ws_trace['info'] = header_part # Fallback
                else:
                    ws_trace['info'] = header_part
            elif msg.startswith("> "):
                ws_trace['direction'] = "outbound"
                header_part = msg[2:]
                if ":" in header_part:
                    try:
                        key, value = header_part.split(":", 1)
                        ws_trace['header'] = key.strip()
                        ws_trace['value'] = value.strip()
                    except ValueError:
                        ws_trace['info'] = header_part # Fallback
                else:
                    ws_trace['info'] = header_part
            elif msg.startswith("= "):
                ws_trace['direction'] = "state"
                ws_trace['info'] = msg[2:]
            elif msg.startswith("! "):
                ws_trace['direction'] = "event"
                ws_trace['info'] = msg[2:]

            if ws_trace:
                log_entry['websocket_trace'] = ws_trace

        # Add all dynamic fields from 'extra'
        reserved_attrs = {
            'args', 'asctime', 'created', 'exc_info', 'exc_text', 'filename',
            'funcName', 'levelname', 'levelno', 'lineno', 'module', 'msecs',
            'message', 'msg', 'name', 'pathname', 'process', 'processName',
            'relativeCreated', 'stack_info', 'thread', 'threadName', 'taskName'
        }
        for key, value in record.__dict__.items():
            if key not in reserved_attrs and key not in log_entry:
                log_entry[key] = value

        return json.dumps(log_entry, default=str)

def setup_logger():
    """Sets up the root logger to use JSONFormatter."""
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    # Remove any existing handlers
    for handler in logger.handlers[:]:
        logger.removeHandler(handler)

    handler = logging.StreamHandler()
    formatter = JSONFormatter()
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    # Enable debug logging for websockets library if configured
    if config.DEBUG_WEBSOCKETS:
        handler.setLevel(logging.DEBUG) # Ensure handler processes DEBUG messages
        ws_logger = logging.getLogger("websockets")
        ws_logger.setLevel(logging.DEBUG)
        ws_logger.addHandler(handler)
        ws_logger.propagate = False # Prevent double logging to root

        ws_protocol_logger = logging.getLogger("websockets.protocol")
        ws_protocol_logger.setLevel(logging.DEBUG)
        ws_protocol_logger.addHandler(handler)
        ws_protocol_logger.propagate = False
        logger.info("DEBUG logging enabled for 'websockets' library.", extra={"log_type": "config"})
    else:
        logging.getLogger("websockets").setLevel(logging.INFO)
