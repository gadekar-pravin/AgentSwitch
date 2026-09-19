"""Small synchronous OpenRouter chat-completions client."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from http.client import HTTPException
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .config import Config
from .mcp_client import _read_env_file

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

Transport = Callable[[str, bytes, dict[str, str], float], tuple[int, bytes]]


class OpenRouterError(Exception):
    """OpenRouter rejected a request or returned an invalid response."""

    usage: dict[str, Any] | None = None


class OpenRouterRetryable(OpenRouterError):
    """One OpenRouter attempt failed in a way the metered seam may retry."""


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


def _with_usage(error: OpenRouterError, response: dict[str, Any]) -> OpenRouterError:
    usage = response.get("usage")
    if isinstance(usage, dict):
        error.usage = usage
    return error


class OpenRouterClient:
    """Make one OpenRouter attempt without exposing the API key in diagnostics."""

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        reasoning_effort: str,
        seed: int,
        max_tokens: int,
        timeout: float,
        transport: Transport | None = None,
    ) -> None:
        if not isinstance(api_key, str) or not api_key:
            raise ValueError("api_key must be a non-empty string")
        if not isinstance(model, str) or not model:
            raise ValueError("model must be a non-empty string")
        self._api_key = api_key
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.seed = seed
        self.max_tokens = max_tokens
        self.timeout = timeout
        self._transport = transport or _default_transport

    def __repr__(self) -> str:
        safe_model = self.model.replace(self._api_key, "<redacted>")
        return f"{type(self).__name__}(model={safe_model!r})"

    def redaction_secret(self) -> str:
        """Return the credential solely so persistence boundaries can redact it."""
        return self._api_key

    def build_body(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = None,
        extra: dict[str, Any] | None = None,
    ) -> bytes:
        """Build the exact compact JSON request body sent by :meth:`chat`."""
        payload: dict[str, Any] = {}
        if extra:
            payload.update(extra)
        payload.update(
            {
                "model": self.model,
                "messages": messages,
                "provider": {"data_collection": "deny", "require_parameters": True},
                "reasoning": {"effort": self.reasoning_effort},
                "seed": self.seed,
                "max_tokens": self.max_tokens,
            }
        )
        if tools is not None:
            payload["tools"] = tools
        if tool_choice is not None:
            payload["tool_choice"] = tool_choice
        payload.pop("temperature", None)
        return json.dumps(payload, separators=(",", ":")).encode()

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: Any = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        body = self.build_body(
            messages,
            tools=tools,
            tool_choice=tool_choice,
            extra=extra,
        )
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            status, response_body = self._transport(
                OPENROUTER_URL,
                body,
                headers,
                self.timeout,
            )
            if not isinstance(status, int) or isinstance(status, bool) or not isinstance(
                response_body, bytes
            ):
                raise OpenRouterError("Transport must return an integer status and bytes body")
        except (URLError, TimeoutError, OSError, HTTPException) as error:
            raise OpenRouterRetryable(
                f"OpenRouter network failure ({type(error).__name__})"
            ) from None

        if status < 200 or status >= 300:
            excerpt = _safe_text(response_body, self._api_key)
            error_type = OpenRouterRetryable if status == 429 or 500 <= status <= 599 else OpenRouterError
            raise error_type(f"OpenRouter HTTP {status}; body excerpt: {excerpt}")
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
            raise _with_usage(
                OpenRouterError(f"OpenRouter request error: {rendered[:300]}"), response
            )
        choices = response.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise _with_usage(
                OpenRouterError("OpenRouter response did not contain choices"), response
            )
        choice = choices[0]
        if choice.get("finish_reason") == "error" or "error" in choice:
            detail = choice.get("error", "finish_reason=error")
            rendered = json.dumps(detail, ensure_ascii=False)
            if self._api_key:
                rendered = rendered.replace(self._api_key, "<redacted>")
            raise _with_usage(
                OpenRouterRetryable(f"OpenRouter choice error: {rendered[:300]}"),
                response,
            )
        message = choice.get("message")
        if not isinstance(message, dict):
            raise _with_usage(
                OpenRouterError("OpenRouter choice did not contain an assistant message"),
                response,
            )
        usage = response.get("usage")
        returned = {
            "message": message,
            "usage": usage if isinstance(usage, dict) else {},
            "provider": response.get("provider"),
            "model": response.get("model", self.model),
            "finish_reason": choice.get("finish_reason"),
        }
        return _redact_value(returned, self._api_key)


def from_config(
    config: Config,
    *,
    env_file: str | os.PathLike[str] = ".env",
    transport: Transport | None = None,
) -> OpenRouterClient:
    """Construct a client from resolved model config and an environment API key."""
    if "OPENROUTER_API_KEY" in os.environ:
        api_key = os.environ["OPENROUTER_API_KEY"]
    else:
        api_key = _read_env_file(str(env_file)).get("OPENROUTER_API_KEY", "")
    if not api_key:
        raise ValueError("Missing required configuration variable OPENROUTER_API_KEY")
    return OpenRouterClient(
        api_key,
        config.models.agent,
        reasoning_effort=config.models.reasoning_effort,
        seed=config.models.seed,
        max_tokens=config.models.max_tokens,
        timeout=float(config.models.timeout_seconds),
        transport=transport,
    )


__all__ = [
    "OPENROUTER_URL",
    "OpenRouterClient",
    "OpenRouterError",
    "OpenRouterRetryable",
    "Transport",
    "from_config",
]
