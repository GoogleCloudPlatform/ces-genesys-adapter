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
