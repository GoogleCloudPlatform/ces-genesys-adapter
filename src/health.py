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
from collections import deque
import logging
import os
import signal
import time
from typing import Dict, Tuple

from .auth import auth_provider

logger = logging.getLogger(__name__)

# Configurable environment variable thresholds
HEALTH_WINDOW_SECONDS = int(os.getenv("HEALTH_WINDOW_SECONDS", 60))
HEALTH_MIN_SAMPLES = int(os.getenv("HEALTH_MIN_SAMPLES", 15))
MAX_CES_ERROR_RATE_PCT = float(os.getenv("MAX_CES_ERROR_RATE_PCT", 25.0))
MAX_CES_AVG_LATENCY_MS = float(os.getenv("MAX_CES_AVG_LATENCY_MS", 1500.0))
MAX_EVENT_LOOP_LAG_MS = float(os.getenv("MAX_EVENT_LOOP_LAG_MS", 500.0))


class RollingMetrics:
    """Tracks events in a rolling sliding window of N seconds."""

    def __init__(self, window_seconds: int = 60):
        self.window_seconds = window_seconds
        self.events = deque()  # Stores tuples of (timestamp, is_error, latency_ms)

    def add_event(self, is_error: bool, latency_ms: float):
        now = time.monotonic()
        self.events.append((now, is_error, latency_ms))
        self._cleanup(now)

    def _cleanup(self, now: float):
        cutoff = now - self.window_seconds
        while self.events and self.events[0][0] < cutoff:
            self.events.popleft()

    def get_stats(self) -> Dict[str, float]:
        now = time.monotonic()
        self._cleanup(now)
        count = len(self.events)
        if count == 0:
            return {
                "sample_count": 0,
                "qps": 0.0,
                "error_rate_pct": 0.0,
                "avg_latency_ms": 0.0,
            }

        errors = sum(1 for _, is_error, _ in self.events if is_error)
        total_latency = sum(lat for _, _, lat in self.events)

        qps = round(count / self.window_seconds, 2)
        error_rate_pct = round((errors / count) * 100.0, 2)
        avg_latency_ms = round(total_latency / count, 2)

        return {
            "sample_count": count,
            "qps": qps,
            "error_rate_pct": error_rate_pct,
            "avg_latency_ms": avg_latency_ms,
        }


class HealthChecker:
    def __init__(self):
        self.is_draining = False
        self.ces_metrics = RollingMetrics(window_seconds=HEALTH_WINDOW_SECONDS)
        self.genesys_metrics = RollingMetrics(window_seconds=HEALTH_WINDOW_SECONDS)

    def set_draining(self, *args):
        logger.info(
            "SIGTERM/SIGINT received. Marking container as draining.",
            extra={"log_type": "shutdown"},
        )
        self.is_draining = True

    def register_signal_handlers(self, loop=None):
        if loop is None:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
        try:
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, self.set_draining)
        except (NotImplementedError, RuntimeError):
            pass

    def record_ces_event(self, latency_ms: float, is_error: bool = False):
        self.ces_metrics.add_event(is_error=is_error, latency_ms=latency_ms)

    def record_genesys_event(self, latency_ms: float, is_error: bool = False):
        self.genesys_metrics.add_event(is_error=is_error, latency_ms=latency_ms)

    async def check_event_loop_lag(self) -> Tuple[bool, float]:
        start = time.monotonic()
        await asyncio.sleep(0)
        elapsed_ms = (time.monotonic() - start) * 1000.0
        return elapsed_ms <= MAX_EVENT_LOOP_LAG_MS, round(elapsed_ms, 2)

    async def check_auth_health(self) -> bool:
        try:
            token = await asyncio.wait_for(auth_provider.get_token(), timeout=2.0)
            return bool(token)
        except Exception:
            return False

    async def evaluate_health(self) -> Tuple[bool, Dict]:
        ces_stats = self.ces_metrics.get_stats()
        genesys_stats = self.genesys_metrics.get_stats()
        loop_ok, loop_lag_ms = await self.check_event_loop_lag()
        auth_ok = await self.check_auth_health()

        stats = {
            "draining": self.is_draining,
            "event_loop_lag_ms": loop_lag_ms,
            "auth_healthy": auth_ok,
            "ces_metrics": ces_stats,
            "genesys_metrics": genesys_stats,
        }

        # Container-local Health Gates ONLY
        if self.is_draining:
            stats["unhealthy_reason"] = "container_draining"
            return False, stats

        if not loop_ok:
            stats["unhealthy_reason"] = f"event_loop_lag_exceeded_{loop_lag_ms}ms"
            return False, stats

        if not auth_ok:
            stats["unhealthy_reason"] = "auth_health_failed"
            return False, stats

        # CES and Genesys metrics are tracked and reported in stats, but DO NOT fail /health
        return True, stats


health_checker = HealthChecker()
