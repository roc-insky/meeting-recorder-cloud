"""Ephemeral AI/OCR execution with a bounded wait.

A provider is always injected. This module never reads a URL, opens a socket,
writes a prompt or response to disk, or keeps a global executor/queue/task
registry: one request gets one worker thread that is discarded afterwards.

A Python thread cannot be killed. On timeout the caller stops waiting, cancels
the future and abandons the thread; the provider call may keep running in the
background until it returns. This limitation is intentional and documented
rather than hidden behind a process pool that would add persistence.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from typing import Any, Protocol, Sequence

from cloud.contracts import ServiceUnavailableError
from cloud.settings import EPHEMERAL_TIMEOUT_S


class AIProvider(Protocol):
    """A text/image generation provider."""

    def generate(
        self, system: str, user: str, images: Sequence[bytes]
    ) -> str: ...


class OCRProvider(Protocol):
    """An image-to-text provider."""

    def extract(self, images: Sequence[bytes]) -> str: ...


class EphemeralExecutor:
    """Run one provider call on a fresh thread and wait at most timeout_s."""

    def __init__(
        self,
        provider: AIProvider | OCRProvider,
        timeout_s: int = EPHEMERAL_TIMEOUT_S,
    ) -> None:
        if timeout_s < 1:
            raise ValueError("timeout_s must be positive")
        self._provider = provider
        self._timeout_s = timeout_s

    def run(
        self,
        system: str,
        user: str,
        images: Sequence[bytes] = (),
    ) -> str:
        provider = self._provider
        generate = getattr(provider, "generate", None)
        if generate is None:
            raise ServiceUnavailableError("ai_not_configured")
        payload = list(images)
        return self._wait(lambda: generate(system, user, payload))

    def run_ocr(self, images: Sequence[bytes]) -> str:
        provider = self._provider
        extract = getattr(provider, "extract", None)
        if extract is None:
            raise ServiceUnavailableError("ai_not_configured")
        payload = list(images)
        return self._wait(lambda: extract(payload))

    def _wait(self, call: Any) -> str:
        pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="cloud-ai-ephemeral"
        )
        future = pool.submit(call)
        try:
            return future.result(timeout=self._timeout_s)
        except FutureTimeoutError:
            future.cancel()
            # Do not wait for a stuck provider; abandon the thread.
            pool.shutdown(wait=False, cancel_futures=True)
            raise ServiceUnavailableError("ai_timeout") from None
        except BaseException:
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        else:
            pool.shutdown(wait=True)
