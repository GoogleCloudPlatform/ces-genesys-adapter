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

"""Paces agent audio to Genesys at real-time speed.

Genesys plays whatever it receives, so sending a whole utterance at once
fills its buffer and makes barge-in slow. The pacer keeps about
`PRIME_MS` of audio queued in Genesys and then sends what has played since
the previous send.
"""

import asyncio
from collections.abc import Awaitable, Callable
import logging

import websockets

from src.backends import base

logger = logging.getLogger(__name__)

SAMPLE_RATE_HZ = 8000  # PCMU: one byte per sample.
SEND_INTERVAL_S = 0.28
MAX_CHUNK_BYTES = 16000  # Safety cap; the protocol limit is 64 KiB.
PRIME_MS = 500  # Cushion kept in Genesys to absorb jitter.
PRIME_BYTES = PRIME_MS * SAMPLE_RATE_HZ // 1000
MIN_FRAME_BYTES = 160  # 20 ms; smaller sends are deferred.
_QUEUE_GET_TIMEOUT_S = 0.05
_IDLE_SLEEP_S = 0.01


class OutboundPacer:
    """Queues agent audio and sends it to Genesys at real-time pace."""

    def __init__(
        self,
        send: Callable[[bytes], Awaitable[None]],
        sink_open: Callable[[], bool],
        log_extra: base.LogExtra,
        on_error: Callable[[str], Awaitable[None]],
    ):
        """Initializes the pacer.

        Args:
          send: Sends one binary frame to Genesys.
          sink_open: Returns whether the Genesys socket can still be used.
          log_extra: Builds the `extra` dict for log lines.
          on_error: Called with a description after an unexpected error.
        """
        self._send = send
        self._sink_open = sink_open
        self._log_extra = log_extra
        self._on_error = on_error
        self._queue: asyncio.Queue[bytes] = asyncio.Queue()
        self._buffer = bytearray()
        self._stop_event = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        # Set once the agent has ended the session: the pacer then exits as
        # soon as everything queued has been sent.
        self.end_of_stream = False

    @property
    def queued_bytes(self) -> int:
        """Bytes waiting in the send buffer (excluding the queue)."""
        return len(self._buffer)

    @property
    def queued_chunks(self) -> int:
        """Chunks waiting in the queue."""
        return self._queue.qsize()

    def start(self) -> None:
        """Starts the send loop as a task."""
        self._task = asyncio.create_task(self.run())

    def enqueue(self, chunk: bytes) -> None:
        """Queues agent audio for sending.

        Args:
          chunk: PCMU audio at 8 kHz.
        """
        self._queue.put_nowait(chunk)

    def clear_for_barge_in(self) -> None:
        """Discards queued and buffered audio after a barge-in."""
        cleared_chunks = self._drain_queue()
        cleared_bytes = len(self._buffer)
        self._buffer.clear()
        logger.info(
            "Cleared audio output queue and pacer buffer due to"
            " InterruptionSignal",
            extra=self._log_extra(
                "ces_recv_interruption_clear",
                {
                    "cleared_queue_chunks": cleared_chunks,
                    "cleared_buffer_bytes": cleared_bytes,
                },
            ),
        )

    async def cancel_task(self) -> None:
        """Cancels the send loop, keeping queued audio for a restart."""
        if self._task is None or self._task.done():
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass

    async def wait_drained(self) -> None:
        """Waits for the send loop to exit on its own."""
        if self._task is None:
            return
        try:
            await self._task
        except Exception as err:  # pylint: disable=broad-exception-caught
            logger.error(
                "Error waiting for pacer to drain: %s",
                err,
                exc_info=True,
                extra=self._log_extra("ces_finalize_error", None),
            )

    async def stop(self) -> None:
        """Stops the send loop and discards all queued audio."""
        logger.info(
            "Stopping audio pacer and clearing queues",
            extra=self._log_extra("ces_pacer_stop", None),
        )
        self._stop_event.set()
        # A pacer error can lead to stop() being called from the pacer task
        # itself; it must not cancel or await itself.
        if self._task is not None and self._task is not asyncio.current_task():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                logger.info(
                    "Pacer task cancelled as expected",
                    extra=self._log_extra("ces_pacer_stop", None),
                )
            except Exception:  # pylint: disable=broad-exception-caught
                logger.error(
                    "Error during pacer task cancellation",
                    exc_info=True,
                    extra=self._log_extra("ces_pacer_error", None),
                )
            self._task = None
        self._buffer.clear()
        cleared = self._drain_queue()
        if cleared:
            logger.info(
                "Audio OUTBOUND queue cleared: discarded %s chunks",
                cleared,
                extra=self._log_extra(
                    "ces_pacer_stop", {"cleared_count": cleared}
                ),
            )
        else:
            logger.info(
                "Audio OUTBOUND queue is empty",
                extra=self._log_extra("ces_pacer_stop", None),
            )

    def _drain_queue(self) -> int:
        cleared = 0
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
                self._queue.task_done()
            except (asyncio.QueueEmpty, ValueError):
                break
            cleared += 1
        return cleared

    async def run(self) -> None:
        """Runs the send loop until stopped, drained or the sink closes."""
        logger.info(
            "Starting audio pacer for Genesys",
            extra=self._log_extra("ces_pacer_start", None),
        )
        self._buffer.clear()
        try:
            await self._send_loop()
        except asyncio.CancelledError:
            logger.info(
                "Pacer task cancelled",
                extra=self._log_extra("ces_pacer_cancelled", None),
            )
            raise
        except websockets.exceptions.ConnectionClosed:
            logger.info(
                "Genesys websocket connection closed, pacer stopped",
                extra=self._log_extra("ces_pacer_connection_closed", None),
            )
        except Exception as err:  # pylint: disable=broad-exception-caught
            logger.error(
                "Unexpected error in pacer",
                exc_info=True,
                extra=self._log_extra("ces_pacer_error", None),
            )
            await self._on_error(f"Pacer Error: {err}")
        logger.info(
            "Audio pacer for Genesys stopped",
            extra=self._log_extra("ces_pacer_stopped", None),
        )

    async def _fill_buffer(self) -> None:
        try:
            chunk = await asyncio.wait_for(
                self._queue.get(), timeout=_QUEUE_GET_TIMEOUT_S
            )
        except asyncio.TimeoutError:
            return  # No new audio; the caller checks whether to send.
        if not chunk:
            return
        self._buffer.extend(chunk)
        try:
            self._queue.task_done()
        except ValueError:
            pass  # The queue was cleared by stop().
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "Pacer added to buffer",
                extra=self._log_extra(
                    "ces_pacer_buffer", {"buffer_size": len(self._buffer)}
                ),
            )

    async def _send_loop(self) -> None:
        loop = asyncio.get_running_loop()
        last_send_time = loop.time()
        primed = False
        while not self._stop_event.is_set():
            if self.end_of_stream and self._queue.empty() and not self._buffer:
                logger.info(
                    "Audio queue drained after endSession, stopping pacer.",
                    extra=self._log_extra("ces_pacer_drained", None),
                )
                return
            await self._fill_buffer()
            now = loop.time()
            elapsed = now - last_send_time

            if not self._buffer:
                if primed:
                    logger.info(
                        "Pacer buffer became empty, resetting primed state",
                        extra=self._log_extra("ces_pacer_empty", None),
                    )
                    primed = False
                await asyncio.sleep(_IDLE_SLEEP_S)
                continue
            if elapsed < SEND_INTERVAL_S:
                continue
            if not self._sink_open():
                logger.warning(
                    "Genesys WS unavailable or closed, breaking pacer loop",
                    extra=self._log_extra("ces_pacer_discard", None),
                )
                self._buffer.clear()
                return

            is_priming_send = not primed
            if is_priming_send:
                chunk_size = min(PRIME_BYTES, len(self._buffer))
                primed = True
                logger.info(
                    "Pacer priming Genesys buffer",
                    extra=self._log_extra(
                        "ces_pacer_prime", {"chunk_size": chunk_size}
                    ),
                )
            else:
                # Send the audio that should have played since the last send.
                chunk_size = min(
                    int(elapsed * SAMPLE_RATE_HZ),
                    len(self._buffer),
                    MAX_CHUNK_BYTES,
                )
            if chunk_size <= 0:
                continue
            if (
                chunk_size < MIN_FRAME_BYTES
                and not self.end_of_stream
                and elapsed < SEND_INTERVAL_S * 2
            ):
                # Avoid tiny frames, but flush a trailing remainder that has
                # waited long enough (end of utterance).
                await asyncio.sleep(_IDLE_SLEEP_S)
                continue
            if not await self._send_chunk(chunk_size):
                return
            if is_priming_send:
                last_send_time = now
            else:
                last_send_time += chunk_size / SAMPLE_RATE_HZ

    async def _send_chunk(self, chunk_size: int) -> bool:
        chunk = bytes(self._buffer[:chunk_size])
        try:
            await self._send(chunk)
        except websockets.exceptions.ConnectionClosed:
            logger.warning(
                "Genesys WS closed during send",
                extra=self._log_extra("ces_pacer_send_error", None),
            )
            return False
        except Exception:  # pylint: disable=broad-exception-caught
            logger.error(
                "Error sending audio to Genesys",
                exc_info=True,
                extra=self._log_extra("ces_pacer_send_error", None),
            )
            return False
        del self._buffer[:chunk_size]
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "Pacer sent to Genesys",
                extra=self._log_extra(
                    "ces_pacer_send", {"audio_size": len(chunk)}
                ),
            )
        return True
