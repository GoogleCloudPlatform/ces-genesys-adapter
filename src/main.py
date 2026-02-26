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
import http
import logging
import sys
import uuid

import websockets

from . import config
from .auth import auth_provider
from .genesys_ws import GenesysWS
from .logging_utils import setup_logger
from .redaction import redact

# Setup JSON logging for the entire application
setup_logger()
logger = logging.getLogger(__name__)
logger.info("Using websockets version", extra={"log_type": "init", "version": websockets.__version__})


def process_request(connection, request):
    """
    This function is called before the WebSocket connection is established.
    It handles /health checks and authenticates WebSocket upgrade requests
    using the modern `websockets` API.
    """
    # Handle /health check endpoint
    if request.path == "/health":
        return connection.respond(http.HTTPStatus.OK, "OK\n")

    # For all other paths, proceed with WebSocket authentication.
    if not auth_provider.verify_request(request):
        logger.info("Request came in", extra={"log_type": "auth", "path": request.path})
        logger.warning("WebSocket connection rejected: invalid API key or signature.", extra={"log_type": "auth_error"})
        return connection.respond(http.HTTPStatus.UNAUTHORIZED, "Unauthorized\n")

    # If authentication is successful, return None to proceed with the handshake.
    logger.info("WebSocket connection authenticated successfully.", extra={"log_type": "auth"})
    return None


async def handler(websocket):
    """
    This function is called for each incoming WebSocket connection.
    """
    adapter_session_id = str(uuid.uuid4())
    logger.info("New connection", extra={"log_type": "connection_start", "remote_address": websocket.remote_address, "adapter_session_id": adapter_session_id})
    genesys_ws = GenesysWS(websocket, adapter_session_id)
    await genesys_ws.handle_connection()


async def main():
    """
    This is the main entry point of the application.
    """
    if not config.GENESYS_API_KEY:
        logger.error("GENESYS_API_KEY environment variable not set.", extra={"log_type": "config_error"})
        sys.exit(1)

    if not config.GENESYS_CLIENT_SECRET:
        logger.error("GENESYS_CLIENT_SECRET environment variable not set. This is required for signature verification.", extra={"log_type": "config_error"})
        sys.exit(1)

    if config.AUTH_TOKEN_SECRET_PATH:
        logger.info(
            "Authenticating to CES using token-based auth",
            extra={"log_type": "config", "secret_path": config.AUTH_TOKEN_SECRET_PATH}
        )
    else:
        logger.info(
            "Authenticating to CES using Application Default Credentials (ADC).",
            extra={"log_type": "config"}
        )

    if config.GENESYS_CLIENT_SECRET:
        logger.info("Genesys signature verification is enabled.", extra={"log_type": "config"})

    logger.info("Starting WebSocket server", extra={"log_type": "init", "port": config.PORT})

    # For older versions of `websockets`, we must catch the exception
    # raised by plain HTTP requests (like health checks) to prevent crashes.
    async with websockets.serve(
        handler, "0.0.0.0", config.PORT, process_request=process_request,
        max_size=4 * 1024 * 1024  # Increase limit to 4 MiB
    ) as server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Server stopped manually.", extra={"log_type": "shutdown"})
