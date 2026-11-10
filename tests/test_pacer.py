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

"""Unit tests for the outbound pacer."""

import asyncio
import unittest
from unittest import mock

from src import pacer


def _log_extra(log_type, data=None):
    del data  # Unused.
    return {"log_type": log_type}


class OutboundPacerTest(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        super().setUp()
        self.sent: list[bytes] = []
        self.sink_open = True
        self.on_error = mock.AsyncMock()
        self.pacer = pacer.OutboundPacer(
            send=self._send,
            sink_open=lambda: self.sink_open,
            log_extra=_log_extra,
            on_error=self.on_error,
        )

    async def _send(self, chunk: bytes) -> None:
        self.sent.append(chunk)

    async def test_first_send_is_the_prime(self):
        self.pacer.enqueue(b"\x7f" * 8000)
        self.pacer.start()
        await asyncio.sleep(pacer.SEND_INTERVAL_S + 0.1)
        await self.pacer.stop()

        self.assertEqual(len(self.sent[0]), pacer.PRIME_BYTES)

    async def test_drains_and_exits_after_end_of_stream(self):
        self.pacer.enqueue(b"\x7f" * 1000)
        self.pacer.end_of_stream = True
        self.pacer.start()

        await asyncio.wait_for(self.pacer.wait_drained(), 2.0)

        self.assertEqual(sum(len(chunk) for chunk in self.sent), 1000)

    async def test_barge_in_clears_queue_and_buffer(self):
        for _ in range(5):
            self.pacer.enqueue(b"\x7f" * 800)

        self.pacer.clear_for_barge_in()

        self.assertEqual(self.pacer.queued_chunks, 0)
        self.assertEqual(self.pacer.queued_bytes, 0)

    async def test_stops_when_sink_closes(self):
        self.sink_open = False
        self.pacer.enqueue(b"\x7f" * 800)
        self.pacer.start()

        await asyncio.wait_for(self.pacer.wait_drained(), 2.0)

        self.assertFalse(self.sent)
        self.on_error.assert_not_awaited()

    async def test_stop_discards_queued_audio(self):
        self.pacer.enqueue(b"\x7f" * 800)
        self.pacer.start()

        await self.pacer.stop()

        self.assertEqual(self.pacer.queued_chunks, 0)
        self.assertEqual(self.pacer.queued_bytes, 0)


if __name__ == "__main__":
    unittest.main()
