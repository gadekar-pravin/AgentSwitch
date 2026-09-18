"""Small synchronous OpenRouter chat-completions client."""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Callable
from http.client import HTTPException
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .mcp_client import _read_env_file

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
REASONING_EFFORT = "medium"
SEED = 4_042_026
MAX_RETRIES = 2
DEFAULT_TIMEOUT = 180.0

Transport = Callable[[str, bytes, dict[str, str], float], tuple[int, bytes]]


class OpenRouterError(Exception):
    """OpenRouter rejected a request or returned an invalid response."""


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(
        self,
        req: Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


_OPENER = build_opener(_NoRedirectHandler())


def _default_transport(
    url: str,
    body: bytes,
    headers: dict[str, str],
    timeout: float,
) -> tuple[int, bytes]:
    request = Request(url, data=body, headers=headers, method="POST")
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return response.status, response.read()
    except HTTPError as error:
        return error.code, error.read()


def _safe_text(body: bytes, secret: str) -> str:
    text = body.decode("utf-8", errors="replace")
    if secret:
        text = text.replace(secret, "<redacted>")
    text = re.sub(r"(?i)Bearer\s+\S+", "Bearer <redacted>", text)
    compact = " ".join(text.split())
    return (compact[:200] + "...") if len(compact) > 200 else (compact or "<empty>")


def _redact_value(value: Any, secret: str) -> Any:
    if isinstance(value, str):
        return value.replace(secret, "<redacted>") if secret else value
    if isinstance(value, list):
        return [_redact_value(item, secret) for item in value]
    if isinstance(value, dict):
        return {key: _redact_value(item, secret) for key, item in value.items()}
    return value


class OpenRouterClient:
    """Call OpenRouter without exposing the API key in values or diagnostics."""

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not isinstance(api_key, str) or not api_key:
            raise ValueError("api_key must be a non-empty string")
        if not isinstance(model, str) or not model:
            raise ValueError("model must be a non-empty string")
        self._api_key = api_key
        self.model = model
        self.timeout = timeout
        self._transport = transport or _default_transport
        self._sleep = sleep

    def __repr__(self) -> str:
        safe_model = self.model.replace(self._api_key, "<redacted>")
        return f"{type(self).__name__}(model={safe_model!r})"

    def redaction_secret(self) -> str:
        """Return the credential solely so persistence boundaries can redact it."""
        return self._api_key

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "provider": {"data_collection": "deny", "require_parameters": True},
            "reasoning": {"effort": REASONING_EFFORT},
            "seed": SEED,
        }
        if extra:
            payload.update(extra)
            payload.update(
                {
                    "model": self.model,
                    "messages": messages,
                    "provider": {"data_collection": "deny", "require_parameters": True},
                    "reasoning": {"effort": REASONING_EFFORT},
                    "seed": SEED,
                }
            )
        if tools is not None:
            payload["tools"] = tools
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
        payload.pop("temperature", None)
        body = json.dumps(payload, separators=(",", ":")).encode()
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        for attempt in range(MAX_RETRIES + 1):
            try:
                status, response_body = self._transport(
                    OPENROUTER_URL,
                    body,
                    headers,
                    self.timeout,
                )
                if not isinstance(status, int) or not isinstance(response_body, bytes):
                    raise OpenRouterError("Transport must return an integer status and bytes body")
            except (URLError, TimeoutError, OSError, HTTPException) as error:
                if attempt < MAX_RETRIES:
                    self._sleep(2**attempt)
                    continue
                raise OpenRouterError(
                    f"OpenRouter network failure ({type(error).__name__})"
                ) from None

            retryable = status == 429 or 500 <= status <= 599
            if retryable and attempt < MAX_RETRIES:
                self._sleep(2**attempt)
                continue
            if status < 200 or status >= 300:
                excerpt = _safe_text(response_body, self._api_key)
                raise OpenRouterError(f"OpenRouter HTTP {status}; body excerpt: {excerpt}")
            try:
                response = json.loads(response_body)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                excerpt = _safe_text(response_body, self._api_key)
                raise OpenRouterError(
                    f"OpenRouter returned invalid JSON ({type(error).__name__}); "
                    f"body excerpt: {excerpt}"
                ) from None
            if not isinstance(response, dict):
                raise OpenRouterError("OpenRouter response must be an object")
            if "error" in response:
                rendered = json.dumps(response["error"], ensure_ascii=False)
                if self._api_key:
                    rendered = rendered.replace(self._api_key, "<redacted>")
                raise OpenRouterError(f"OpenRouter request error: {rendered[:300]}")
            choices = response.get("choices")
            if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
                raise OpenRouterError("OpenRouter response did not contain choices")
            choice = choices[0]
            if choice.get("finish_reason") == "error" or "error" in choice:
                detail = choice.get("error", "finish_reason=error")
                rendered = json.dumps(detail, ensure_ascii=False)
                if self._api_key:
                    rendered = rendered.replace(self._api_key, "<redacted>")
                raise OpenRouterError(f"OpenRouter choice error: {rendered[:300]}")
            message = choice.get("message")
            if not isinstance(message, dict):
                raise OpenRouterError("OpenRouter choice did not contain an assistant message")
            usage = response.get("usage")
            returned = {
                "message": message,
                "usage": usage if isinstance(usage, dict) else {},
                "provider": response.get("provider"),
                "model": response.get("model", self.model),
                "finish_reason": choice.get("finish_reason"),
            }
            return _redact_value(returned, self._api_key)

        raise AssertionError("retry loop terminated unexpectedly")


def from_env(env_file: str = ".env") -> OpenRouterClient:
    """Construct a client from environment variables, falling back to an env file."""
    file_values: dict[str, str] | None = None

    def setting(name: str) -> str:
        nonlocal file_values
        if name in os.environ:
            value = os.environ[name]
        else:
            if file_values is None:
                file_values = _read_env_file(env_file)
            value = file_values.get(name, "")
        if not value:
            raise ValueError(f"Missing required configuration variable {name}")
        return value

    return OpenRouterClient(setting("OPENROUTER_API_KEY"), setting("OPENROUTER_MODEL"))


__all__ = [
    "DEFAULT_TIMEOUT",
    "OPENROUTER_URL",
    "OpenRouterClient",
    "OpenRouterError",
    "REASONING_EFFORT",
    "SEED",
    "from_env",
]
