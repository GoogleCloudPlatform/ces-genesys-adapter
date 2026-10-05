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

import asyncio
from contextlib import asynccontextmanager
import http
import logging
import sys
from typing import Optional
import uuid

from fastapi import FastAPI, WebSocket, status
from fastapi.responses import JSONResponse
import uvicorn
import websockets
from websockets.connection import State

from . import config
from .auth import auth_provider
from .genesys_ws import GenesysWS
from .health import health_checker
from .logging_utils import setup_logger

# Setup JSON logging for the entire application
setup_logger()
logger = logging.getLogger(__name__)


class FastApiWebSocketAdapter:
    """
    Adapter wrapper to bridge FastAPI WebSocket instances with standard
    websockets Protocol interface used across the codebase.
    """

    def __init__(self, websocket: WebSocket):
        self._ws = websocket
        self._rate_limit = 50.0  # max 50 messages per second
        self._tokens = self._rate_limit
        self._last_token_update = None
        self._is_closing = False

    @property
    def client(self):
        return self._ws.client

    @property
    def state(self):
        from fastapi.websockets import WebSocketState

        if self._ws.client_state == WebSocketState.CONNECTED:
            return State.OPEN
        return State.CLOSED

    async def _wait_for_token(self):
        loop = asyncio.get_running_loop()
        now = loop.time()
        
        if self._last_token_update is None:
            self._last_token_update = now
            self._tokens -= 1.0
            return

        while True:
            elapsed = now - self._last_token_update
            self._tokens = min(self._rate_limit, self._tokens + (elapsed * self._rate_limit))
            self._last_token_update = now

            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            
            wait_time = (1.0 - self._tokens) / self._rate_limit
            await asyncio.sleep(max(0.01, wait_time))
            now = loop.time()

    async def send(self, data):
        from fastapi.websockets import WebSocketState
        from starlette.websockets import WebSocketDisconnect
        
        if self._ws.client_state != WebSocketState.CONNECTED or self._is_closing:
            close_frame = websockets.frames.Close(1001, "Connection closing")
            raise websockets.exceptions.ConnectionClosedOK(close_frame, None)
            
        await self._wait_for_token()
        
        try:
            if isinstance(data, str):
                await self._ws.send_text(data)
            elif isinstance(data, (bytes, bytearray)):
                await self._ws.send_bytes(bytes(data))
        except (RuntimeError, WebSocketDisconnect):
            close_frame = websockets.frames.Close(1001, "Runtime disconnect")
            raise websockets.exceptions.ConnectionClosedOK(close_frame, None)

    async def close(self, code=1000, reason=""):
        self._is_closing = True
        try:
            await self._ws.close(code=code, reason=reason)
        except RuntimeError:
            # Swallow 'Cannot call close once a close message has been sent'
            pass
        except Exception:
            pass

    async def recv(self):
        msg = await self._ws.receive()
        if msg["type"] == "websocket.disconnect":
            code = msg.get("code", 1000)
            reason = msg.get("reason", "")
            close_frame = websockets.frames.Close(code, reason)
            if code in (1000, 1001):
                raise websockets.exceptions.ConnectionClosedOK(close_frame, None)
            raise websockets.exceptions.ConnectionClosedError(close_frame, None)
        if "text" in msg and msg["text"] is not None:
            return msg["text"]
        elif "bytes" in msg and msg["bytes"] is not None:
            return msg["bytes"]
        close_frame = websockets.frames.Close(1000, "Closed")
        raise websockets.exceptions.ConnectionClosedOK(close_frame, None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return await self.recv()
        except websockets.exceptions.ConnectionClosedOK:
            raise StopAsyncIteration


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: register signal handlers for graceful shutdown and container draining
    try:
        loop = asyncio.get_running_loop()
        health_checker.register_signal_handlers(loop)
    except Exception:
        pass
    logger.info("FastAPI application started", extra={"log_type": "init"})
    yield
    logger.info("FastAPI application shutting down", extra={"log_type": "shutdown"})


app = FastAPI(title="CES Genesys Adapter", version="2.0.0", lifespan=lifespan)


# 1. Active SRE Golden Signals Health Probe Endpoints
@app.get("/health/liveness")
async def liveness_check():
    is_healthy, stats = await health_checker.evaluate_liveness()
    if not is_healthy:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "unhealthy", "metrics": stats},
        )
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"status": "ok", "metrics": stats},
    )


@app.get("/health/readiness")
async def readiness_check():
    is_healthy, stats = await health_checker.evaluate_readiness()
    if not is_healthy:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"status": "unhealthy", "metrics": stats},
        )
    return JSONResponse(
        status_code=status.HTTP_200_OK,
        content={"status": "ok", "metrics": stats},
    )


@app.websocket("/{path:path}")
async def websocket_endpoint(websocket: WebSocket):

    # Validation 1: Check Container Draining / Auth Health -> 503 Service Unavailable / WS 1013
    if health_checker.is_draining or not await health_checker.check_auth_health():
        await websocket.close(
            code=status.WS_1013_TRY_AGAIN_LATER,
            reason="Service Unavailable",
        )
        return

    # Validation 2: Verify Request Signatures / API Key -> 401 Unauthorized / WS 1008
    if not auth_provider.verify_fastapi_request(websocket):
        logger.warning(
            "WebSocket connection rejected: invalid API key or signature.",
            extra={"log_type": "auth_error"}
        )
        await websocket.close(
            code=status.WS_1008_POLICY_VIOLATION,
            reason="Unauthorized",
        )
        return

    # Accept WebSocket connection and hand over to GenesysWS handler
    await websocket.accept()
    adapted_ws = FastApiWebSocketAdapter(websocket)
    adapter_session_id = str(uuid.uuid4())
    logger.info(
        "New WebSocket connection accepted via FastAPI",
        extra={
            "log_type": "connection_start",
            "remote_address": str(websocket.client),
            "adapter_session_id": adapter_session_id,
        },
    )
    genesys_ws = GenesysWS(adapted_ws, adapter_session_id=adapter_session_id)
    await genesys_ws.handle_connection()


def main():
    if not config.GENESYS_API_KEY:
        logger.error(
            "GENESYS_API_KEY environment variable not set.",
            extra={"log_type": "config_error"},
        )
        sys.exit(1)

    if not config.GENESYS_CLIENT_SECRET:
        logger.error(
            "GENESYS_CLIENT_SECRET environment variable not set. This is required for signature verification.",
            extra={"log_type": "config_error"},
        )
        sys.exit(1)

    port = int(config.PORT)
    logger.info(
        "Starting FastAPI server with Uvicorn",
        extra={"log_type": "init", "port": port},
    )
    uvicorn.run("src.main:app", host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
