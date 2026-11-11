"""All web requests go through here: polite pacing, limited parallelism, and retries.

- Retries: a request that fails with a dropped connection, a timeout, or a server error
  (429/500/502/503/504) is retried up to 3 times, waiting 5, 15 and then 45 seconds
  (or as long as the site asks, via Retry-After, up to 60 seconds).
- Timeout: 10 seconds to connect, 60 seconds to read.
- Parallelism: at most 6 requests at once overall. Sites that are strict about automated
  readers (SLOW_HOSTS below) get one request at a time, at least 2
  seconds apart; other sites at most 2 at a time, at least 0.5 seconds apart.
"""
from __future__ import annotations

import threading
import time
from typing import Dict, Optional
from urllib.parse import urlsplit

import requests

RETRY_WAITS = (5, 15, 45)
TIMEOUT = (10, 60)                       # (connect, read) seconds
MAX_PARALLEL = 6
SLOW_HOSTS = ("amazon.jobs", "google.com", "apple.com", "myworkdayjobs.com", "myworkdaysite.com")
RETRY_STATUS = {429, 500, 502, 503, 504}
RETRY_ERRORS = (requests.exceptions.ConnectionError, requests.exceptions.Timeout,
                requests.exceptions.ChunkedEncodingError)

_all = threading.BoundedSemaphore(MAX_PARALLEL)
_lock = threading.Lock()
_per_host: Dict[str, threading.BoundedSemaphore] = {}
_next_slot: Dict[str, float] = {}


def _slow(host: str) -> bool:
    return any(host == h or host.endswith("." + h) for h in SLOW_HOSTS)


def _host_gate(host: str) -> threading.BoundedSemaphore:
    with _lock:
        if host not in _per_host:
            _per_host[host] = threading.BoundedSemaphore(1 if _slow(host) else 2)
        return _per_host[host]


def _wait_turn(host: str) -> None:
    """Space out request starts to the same site."""
    gap = 2.0 if _slow(host) else 0.5
    with _lock:
        now = time.monotonic()
        start = max(now, _next_slot.get(host, 0.0))
        _next_slot[host] = start + gap
    if start > now:
        time.sleep(start - now)


def fetch(method: str, url: str, *, params=None, json_body=None, headers=None,
          timeout=TIMEOUT) -> requests.Response:
    """Make one request with pacing and retries. Returns the final response (the caller
    decides what a 4xx means); raises the last network error if every attempt failed."""
    host = (urlsplit(url).hostname or "").lower()
    last_error: Optional[Exception] = None
    for attempt in range(len(RETRY_WAITS) + 1):
        wait = None
        try:
            with _all, _host_gate(host):
                _wait_turn(host)
                r = requests.request(method, url, params=params, json=json_body, headers=headers, timeout=timeout)
            if r.status_code not in RETRY_STATUS or attempt == len(RETRY_WAITS):
                return r
            after = r.headers.get("Retry-After", "")
            wait = min(int(after), 60) if after.isdigit() else RETRY_WAITS[attempt]
        except RETRY_ERRORS as e:
            last_error = e
            if attempt == len(RETRY_WAITS):
                raise
            wait = RETRY_WAITS[attempt]
        time.sleep(wait)                     # never holds a request slot while waiting
    raise last_error or RuntimeError("request failed")
