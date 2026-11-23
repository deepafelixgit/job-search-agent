"""All web requests go through here: polite pacing, limited parallelism, and retries.

- Retries: a request that fails with a dropped connection, a timeout, or a server error
  (500/502/503/504) is retried up to 3 times, waiting 5, 15 and then 45 seconds
  (or as long as the site asks, via Retry-After, up to 60 seconds).
- "Too many requests" (429): the site is asking us to slow down. We wait as long as it says
  (Retry-After, up to 5 minutes), or 60 seconds when it doesn't say, and try ONCE more. If it
  refuses again (or asks for more than 5 minutes), the 429 is returned and the reader stops
  reading that company for this run (keeping what it has read so far).
- Timeout: 10 seconds to connect, 60 seconds to read.
- Parallelism: at most 6 requests at once overall. Sites that are strict about automated
  readers (SLOW_HOSTS below) get one request at a time, at least 2
  seconds apart; other sites at most 2 at a time, at least 0.5 seconds apart.
"""
from __future__ import annotations

import datetime as dt
import threading
import time
from email.utils import parsedate_to_datetime
from typing import Dict, Optional
from urllib.parse import urlsplit

import requests

RETRY_WAITS = (5, 15, 45)
TIMEOUT = (10, 60)                       # (connect, read) seconds
MAX_PARALLEL = 6
SLOW_HOSTS = ("amazon.jobs", "google.com", "apple.com", "myworkdayjobs.com", "myworkdaysite.com",
              "careers.microsoft.com")
RETRY_STATUS = {500, 502, 503, 504}
MAX_RETRY_AFTER = 300                    # seconds: the longest Retry-After we'll wait for a 429
WAIT_429 = 60                            # seconds to wait for a 429 that doesn't say how long
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


_log_hosts: Dict[str, str] = {}          # host -> log file path (request logging for diagnosis)


def log_requests(host: str, path: str) -> None:
    """Log every request to `host` (time, status, Retry-After, URL) to the file at `path`."""
    with _lock:
        _log_hosts[host.lower()] = path


def _log_request(host: str, r: requests.Response) -> None:
    path = next((p for h, p in _log_hosts.items() if host == h or host.endswith("." + h)), None)
    if not path:
        return
    line = (f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{r.status_code}\t"
            f"Retry-After={r.headers.get('Retry-After', '-')}\t{r.request.method} {r.url}\n")
    with _lock, open(path, "a", encoding="utf-8") as f:
        f.write(line)


def retry_after_seconds(value: Optional[str]) -> Optional[float]:
    """Retry-After as seconds: a number of seconds or an HTTP date. None if missing or unreadable."""
    value = (value or "").strip()
    if not value:
        return None
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    return max(0.0, (when - dt.datetime.now(dt.timezone.utc)).total_seconds())


def fetch(method: str, url: str, *, params=None, json_body=None, headers=None,
          timeout=TIMEOUT) -> requests.Response:
    """Make one request with pacing and retries. Returns the final response (the caller
    decides what a 4xx means); raises the last network error if every attempt failed."""
    host = (urlsplit(url).hostname or "").lower()
    attempt, waited_for_429 = 0, False
    while True:
        try:
            with _all, _host_gate(host):
                _wait_turn(host)
                r = requests.request(method, url, params=params, json=json_body, headers=headers, timeout=timeout)
        except RETRY_ERRORS:
            if attempt >= len(RETRY_WAITS):
                raise
            time.sleep(RETRY_WAITS[attempt])  # never holds a request slot while waiting
            attempt += 1
            continue
        _log_request(host, r)
        if r.status_code == 429:
            wait = retry_after_seconds(r.headers.get("Retry-After"))
            wait = WAIT_429 if wait is None else wait
            if waited_for_429 or wait > MAX_RETRY_AFTER:
                return r                     # stop: the reader keeps what it has and stops this company
            waited_for_429 = True
            time.sleep(wait)
            continue
        if r.status_code in RETRY_STATUS and attempt < len(RETRY_WAITS):
            after = retry_after_seconds(r.headers.get("Retry-After"))
            time.sleep(min(after, 60) if after is not None else RETRY_WAITS[attempt])
            attempt += 1
            continue
        return r
