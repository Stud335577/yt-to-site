"""Shared Groq SDK client, request pacing and bounded retry policy."""
from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable, TypeVar

from groq import Groq, APIConnectionError, APITimeoutError, APIStatusError

T = TypeVar("T")
_lock = threading.Lock()
_last_request: float | None = None
_min_interval = 3.0


def configure_rate_limit(seconds: float) -> None:
    """Set the process-wide minimum interval between Groq request starts."""
    global _min_interval
    if seconds < 0:
        raise ValueError("Groq request interval must be nonnegative")
    _min_interval = seconds


def _pace() -> None:
    global _last_request
    with _lock:
        now = time.monotonic()
        if _last_request is not None:
            wait = _min_interval - (now - _last_request)
            if wait > 0:
                time.sleep(wait)
        _last_request = time.monotonic()


def _retry_after(exc: Exception) -> float:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", {}) if response is not None else {}
    value = headers.get("retry-after")
    if not value:
        return 0.0
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            return max(0.0, (date - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return 0.0


def _retryable(exc: Exception) -> bool:
    if isinstance(exc, (APIConnectionError, APITimeoutError)):
        return True
    return isinstance(exc, APIStatusError) and (exc.status_code == 429 or exc.status_code >= 500)


def request(operation: Callable[[], T], attempts: int = 4) -> T:
    """Pace every attempt, retry 429/5xx/network errors and honor Retry-After."""
    for attempt in range(attempts):
        _pace()
        try:
            return operation()
        except Exception as exc:
            if not _retryable(exc) or attempt == attempts - 1:
                raise
            delay = max(3.0 * (2 ** attempt), _retry_after(exc))
            # Do not unexpectedly sleep for hours on a daily quota reset.
            if delay > 300:
                raise RuntimeError(f"Groq retry delay is {delay:.0f}s; resume the pipeline later") from exc
            time.sleep(delay)
    raise RuntimeError("Groq request attempts exhausted")


def client(api_key: str | None = None) -> Groq:
    key = api_key or os.getenv("GROQ_API_KEY")
    if not key:
        raise ValueError("GROQ_API_KEY required")
    # Retry behavior is handled centrally so every attempt is paced.
    return Groq(api_key=key, max_retries=0, timeout=180.0)


def chat(messages: list[dict], model: str, api_key: str, max_tokens: int = 2048,
         temperature: float = 0.3) -> str:
    api = client(api_key)
    result = request(lambda: api.chat.completions.create(
        model=model, messages=messages, max_tokens=max_tokens, temperature=temperature,
    ))
    if not result.choices:
        raise ValueError("Groq returned no completion")
    choice = result.choices[0]
    if choice.finish_reason == "length":
        raise ValueError("Groq truncated the response; reduce the input chunk size and retry")
    text = choice.message.content
    if not text or not text.strip():
        raise ValueError("Groq returned an empty completion")
    return text.strip()


def transcribe_file(path: Path, language: str, model: str, api_key: str) -> str:
    api = client(api_key)
    def upload():
        # Reopen on every attempt so retries never upload a consumed stream.
        with path.open("rb") as audio:
            return api.audio.transcriptions.create(
                file=(path.name, audio, "audio/mpeg"), model=model,
                language=language, response_format="json",
            )
    result = request(upload)
    return (result.text or "").strip()


def split_text(text: str, max_chars: int = 3000) -> list[str]:
    """Split on whitespace to keep prompts bounded without cutting tokens or words."""
    chunks = []
    remaining = text.strip()
    while len(remaining) > max_chars:
        cut = max(remaining.rfind(" ", 0, max_chars + 1), remaining.rfind("\n", 0, max_chars + 1),
                  remaining.rfind("\t", 0, max_chars + 1))
        if cut <= 0:
            raise ValueError("Cannot split a long unbroken input safely")
        chunks.append(remaining[:cut].strip())
        remaining = remaining[cut:].strip()
    if remaining:
        chunks.append(remaining)
    return chunks
