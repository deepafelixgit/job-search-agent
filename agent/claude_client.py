"""The ONLY place that talks to the Claude API.

The API key is read from .env into memory, handed straight to the official
Anthropic library, and never printed, logged, or written anywhere else.
"""
from __future__ import annotations

import json
import time
from typing import Any, List, Optional

import anthropic
from dotenv import dotenv_values

from .config import ROOT

# Server-side fallback: if a model declines a request, the API can retry it
# on a suitable fallback model automatically.
FALLBACK_BETA = "server-side-fallback-2026-07-01"
WEB_SEARCH_TOOL = {"type": "web_search_20260209", "name": "web_search", "max_uses": 5}

_client: Optional[anthropic.Anthropic] = None


class ClaudeError(RuntimeError):
    """A readable error that never contains the API key."""


def get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        key = (dotenv_values(ROOT / ".env").get("ANTHROPIC_API_KEY") or "").strip()
        if not key:
            raise ClaudeError("ANTHROPIC_API_KEY is missing from the .env file.")
        _client = anthropic.Anthropic(api_key=key, max_retries=4, timeout=600)
    return _client


def _call(**kwargs) -> Any:
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
    if resp.stop_reason == "refusal":
        raise ClaudeError("Claude declined this request.")
    return resp


def _text(resp) -> str:
    return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")


def ask_json(model: str, system: Any, prompt: str, schema: dict, effort: str = "high",
             max_tokens: int = 16000) -> dict:
    """Ask Claude for an answer that is guaranteed to match `schema`."""
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


def ask_with_web_search(model: str, prompt: str, effort: str = "medium") -> tuple[str, List[dict]]:
    """Let Claude search the web. Returns (final text, list of real search results)."""
    messages: List[dict] = [{"role": "user", "content": prompt}]
    results: List[dict] = []
    final_text = ""
    for _ in range(6):  # long searches can pause; continue them a few times
        resp = _call(
            model=model, max_tokens=16000, thinking={"type": "adaptive"},
            output_config={"effort": effort}, tools=[WEB_SEARCH_TOOL], messages=messages,
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
