"""Bounded retry decisions for HTTP delivery; callers retain the operation identity."""
from __future__ import annotations

import math
import random
import time
from email.utils import parsedate_to_datetime

import httpx

from .service import DomainError


def retry_delay(error: Exception, attempt: int) -> float | None:
    """None means permanent failure. Retry-After is a minimum, not a suggestion."""
    status = error.response.status_code if isinstance(error, httpx.HTTPStatusError) else (
        error.status if isinstance(error, DomainError) else None)
    if status is not None and status < 500 and status not in {408, 425, 429}:
        return None
    base = min(2 ** min(attempt, 9), 300)
    delay = random.uniform(base / 2, base)
    if isinstance(error, httpx.HTTPStatusError):
        value = error.response.headers.get('Retry-After', '')
        try:
            seconds = float(value)
        except ValueError:
            try:
                seconds = parsedate_to_datetime(value).timestamp() - time.time()
            except (ValueError, TypeError, OverflowError):
                seconds = 0
        if math.isfinite(seconds):
            delay = max(delay, seconds)
    return delay
