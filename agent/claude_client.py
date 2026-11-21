"""The ONLY place that talks to the Claude API.

The API key is read from .env into memory, handed straight to the official
Anthropic library, and never printed, logged, or written anywhere else.
"""
from __future__ import annotations

import datetime as dt
import json
import threading
import time
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

from .config import ROOT, LOG_DIR

# The anthropic library and the .env file are loaded only when an API call is actually made, so
# code that merely imports this module (the application renderer, the document builders) never
# loads the API library or reads the API key.

# Server-side fallback: if a model declines a request, the API can retry it
# on a suitable fallback model automatically.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search", "max_uses": 5}

_client = None


class ClaudeError(RuntimeError):
    """A readable error that never contains the API key."""


# ------------------------------------------------------------------ cost meter
# USD per million tokens: (input, output, cache read). Cache writes (5-minute) cost 1.25x input.
# Web searches cost $10 per 1,000. Update these if Anthropic's prices change.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.10),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-opus-5": (5.00, 25.00, 0.50),
    "claude-sonnet-5": (2.00, 10.00, 0.20),
}
SEARCH_PRICE = 0.01
USAGE_LOG = LOG_DIR / "api_usage.log"

_local = threading.local()      # the meter for the application being written on this thread


class Meter:
    """Adds up what one application costs, step by step."""

    def __init__(self, budget: float):
        self.budget = budget
        self.step = "Other"
        self.rows: Dict[str, dict] = {}     # step -> totals
        self.unpriced: set = set()

    @property
    def spent(self) -> float:
        return sum(r["cost"] for r in self.rows.values())

    def can_afford(self, reserve: float) -> bool:
        """True if an optional extra round fits: spent so far plus `reserve` (what the steps still
        required are expected to cost) stays within the budget."""
        return self.spent + reserve <= self.budget

    def add(self, model: str, u: dict, cost: float) -> None:
        r = self.rows.setdefault(self.step, {"calls": 0, "cost": 0.0, "models": set(), "input": 0,
                                             "cache_read": 0, "cache_write": 0, "output": 0, "searches": 0})
        r["calls"] += 1
        r["cost"] += cost
        r["models"].add(model)
        for k in ("input", "cache_read", "cache_write", "output", "searches"):
            r[k] += u[k]

    def report(self) -> List[str]:
        """Lines for changes.txt."""
        total = self.spent
        out = [f"API COST FOR THIS APPLICATION: ${total:.2f} (budget ${self.budget:.2f}"
               + (", over budget: required steps always run)" if total > self.budget else ")")]
        for step, r in self.rows.items():
            models = ", ".join(sorted(m.replace("claude-", "") for m in r["models"]))
            out.append(f"- {step}: ${r['cost']:.2f} ({r['calls']} call{'s' * (r['calls'] != 1)}; {models}; "
                       f"{r['input']:,} new input + {r['cache_read']:,} cached input tokens, "
                       f"{r['output']:,} output tokens incl. thinking"
                       + (f"; {r['searches']} web searches" if r["searches"] else "") + ")")
        if self.unpriced:
            out.append(f"- Note: no price on file for {', '.join(sorted(self.unpriced))}; counted at Opus 5.5 "
                       "prices. Update PRICES in agent/claude_client.py.")
        return out


def start_meter(budget: float) -> Meter:
    """Start tracking a new application on this thread (replaces any earlier meter)."""
    _local.meter = Meter(budget)
    return _local.meter


def stop_meter() -> None:
    _local.meter = None


def meter() -> Optional[Meter]:
    return getattr(_local, "meter", None)


def set_step(name: str) -> None:
    m = meter()
    if m:
        m.step = name


def current_step() -> str:
    m = meter()
    return m.step if m else "Other"


@contextmanager
def step(name: str):
    """Label the calls inside the block (e.g. "Cover letter") in the cost breakdown."""
    m = meter()
    old = m.step if m else None
    if m:
        m.step = name
    try:
        yield
    finally:
        if m:
            m.step = old


def can_afford(reserve: float = 0.0) -> bool:
    """For optional extra rounds. Always True when no meter is running."""
    m = meter()
    return m.can_afford(reserve) if m else True


def _record(requested_model: str, resp) -> None:
    """Price one response, add it to the meter and append it to data/logs/api_usage.log."""
    usage = getattr(resp, "usage", None)
    if usage is None:
        return
    model = getattr(resp, "model", None) or requested_model     # a fallback model may have answered
    stu = getattr(usage, "server_tool_use", None)
    u = {"input": getattr(usage, "input_tokens", 0) or 0,
         "cache_read": getattr(usage, "cache_read_input_tokens", 0) or 0,
         "cache_write": getattr(usage, "cache_creation_input_tokens", 0) or 0,
         "output": getattr(usage, "output_tokens", 0) or 0,
         "searches": (getattr(stu, "web_search_requests", 0) or 0) if stu else 0}
    price = PRICES.get(model)
    m = meter()
    if price is None:
        price = PRICES["claude-opus-5-5"]
        if m:
            m.unpriced.add(model)
    p_in, p_out, p_read = price
    cost = (u["input"] * p_in + u["cache_write"] * p_in * 1.25 + u["cache_read"] * p_read
            + u["output"] * p_out) / 1e6 + u["searches"] * SEARCH_PRICE
    if m:
        m.add(model, u, cost)
    try:
        with open(USAGE_LOG, "a", encoding="utf-8") as f:
            f.write(f"{dt.datetime.now():%Y-%m-%d %H:%M:%S}\t{m.step if m else 'Other'}\t{model}\t"
                    f"in={u['input']}\tcache_read={u['cache_read']}\tcache_write={u['cache_write']}\t"
                    f"out={u['output']}\tsearches={u['searches']}\t${cost:.4f}\n")
    except OSError:
        pass


def get_client():
    global _client
    import anthropic
    from dotenv import dotenv_values
    if _client is None:
        key = (dotenv_values(ROOT / ".env").get("ANTHROPIC_API_KEY") or "").strip()
        if not key:
            raise ClaudeError("ANTHROPIC_API_KEY is missing from the .env file.")
        _client = anthropic.Anthropic(api_key=key, max_retries=4, timeout=600)
    return _client


def _call(**kwargs) -> Any:
    import anthropic
    try:
        resp = get_client().beta.messages.create(betas=[FALLBACK_BETA], fallbacks="default", **kwargs)
    except anthropic.AuthenticationError:
        raise ClaudeError("Claude rejected the API key in .env (authentication failed).") from None
    except anthropic.PermissionDeniedError:
        raise ClaudeError("The API key in .env doesn't have permission for this model or feature.") from None
    except anthropic.RateLimitError:
        raise ClaudeError("Claude API rate limit reached; try again in a few minutes.") from None
    except anthropic.BadRequestError as e:
        raise ClaudeError(f"Claude API rejected the request: {getattr(e, 'message', 'bad request')}") from None
    except anthropic.APIStatusError as e:
        raise ClaudeError(f"Claude API error (status {e.status_code}). Try again later.") from None
    except anthropic.APIConnectionError:
        raise ClaudeError("Couldn't reach the Claude API (network problem).") from None
    _record(kwargs.get("model", ""), resp)
    if resp.stop_reason == "refusal":
        raise ClaudeError("Claude declined this request.")
    return resp


def _text(resp) -> str:
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")


def ask_json(model: str, system: Any, prompt: str, schema: dict, effort: str = "high",
             max_tokens: int = 16000, cache_context: str = "") -> dict:
    """Ask Claude for an answer that is guaranteed to match `schema`.

    cache_context: material several calls share word for word (evidence, resume, posting). It is sent
    first and cached, so repeat calls pay about 5% of the normal price for it. Calls only share the cache
    when the model, effort and this text are identical."""
    if cache_context:
        system = [{"type": "text", "text": "MATERIAL FOR THIS APPLICATION (your instructions follow it):\n\n"
                   + cache_context, "cache_control": {"type": "ephemeral"}},
                  {"type": "text", "text": system}]
    resp = _call(
        model=model, max_tokens=max_tokens, system=system,
        thinking={"type": "adaptive"},
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": prompt}],
    )
    if resp.stop_reason == "max_tokens":
        raise ClaudeError("Claude's answer was cut off (too long).")
    try:
        return json.loads(_text(resp))
    except ValueError:
        raise ClaudeError("Claude returned an answer that wasn't valid JSON.") from None


def ask_with_web_search(model: str, prompt: str, effort: str = "medium",
                        max_searches: int = 5) -> tuple[str, List[dict]]:
    """Let Claude search the web (at most max_searches searches). Returns (final text, list of real
    search results)."""
    tool = dict(WEB_SEARCH_TOOL, max_uses=max_searches)
    messages: List[dict] = [{"role": "user", "content": prompt}]
    results: List[dict] = []
    final_text = ""
    for _ in range(6):  # long searches can pause; continue them a few times
        resp = _call(
            model=model, max_tokens=16000, thinking={"type": "adaptive"},
            output_config={"effort": effort}, tools=[tool], messages=messages,
        )
        for b in resp.content:
            if getattr(b, "type", "") == "web_search_tool_result" and isinstance(b.content, list):
                for r in b.content:
                    if getattr(r, "url", None):
                        results.append({"url": r.url, "title": getattr(r, "title", "") or "",
                                        "page_age": getattr(r, "page_age", "") or ""})
        final_text = _text(resp)
        if resp.stop_reason != "pause_turn":
            break
        messages = messages + [{"role": "assistant", "content": resp.content}]
        time.sleep(1)
    return final_text, results
