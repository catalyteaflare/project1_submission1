"""Configurable chat-completions adapter; transport is injectable for offline use."""

import json
import math
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .prompt import SYSTEM_PROMPT, build_messages
from .providers import ProviderUnavailable
from .validation import SCHEMA


class ProviderFailure(ProviderUnavailable):
    def __init__(self, code: str, diagnostic: str, *, retryable: bool = False, retry_after: float | None = None):
        super().__init__(diagnostic)
        self.code = code
        self.retryable = retryable
        self.retry_after = retry_after


@dataclass(frozen=True)
class HttpConfig:
    endpoint: str
    model: str
    provider: str = "openai-compatible"
    temperature: float = 0
    max_completion_tokens: int = 2048
    timeout: float = 60
    max_response_bytes: int = 1_048_576

    def __post_init__(self):
        parsed = urlsplit(self.endpoint)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("MODEL_ENDPOINT must be an HTTPS URL without credentials, query or fragment")
        if not self.model or not self.provider:
            raise ValueError("A provider and model ID are required")
        if not math.isfinite(self.timeout) or self.timeout <= 0 or self.max_response_bytes <= 0 or self.max_completion_tokens <= 0:
            raise ValueError("Timeout and response/token limits must be positive")
        if not math.isfinite(self.temperature) or not 0 <= self.temperature <= 2:
            raise ValueError("Temperature must be between 0 and 2")


@dataclass(frozen=True)
class HttpReply:
    status: int
    headers: Mapping[str, str]
    body: bytes


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http_transport(*, endpoint: str, headers: dict, body: bytes, timeout: float, max_response_bytes: int) -> HttpReply:
    request = Request(endpoint, data=body, headers=headers, method="POST")
    opener = build_opener(_NoRedirect())
    try:
        response = opener.open(request, timeout=timeout)
    except HTTPError as error:
        response = error
    with response:
        data = response.read(max_response_bytes + 1)
        if len(data) > max_response_bytes:
            raise ProviderFailure("response-too-large", "Provider response exceeded configured size limit")
        return HttpReply(response.code, dict(response.headers.items()), data)


def _retry_after(headers: Mapping[str, str]) -> float | None:
    value = next((v for k, v in headers.items() if k.lower() == "retry-after"), None)
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0, seconds) if math.isfinite(seconds) else None


class HttpProvider:
    def __init__(self, config: HttpConfig, *, credential: str, transport: Callable = http_transport,
                 prompt: str = SYSTEM_PROMPT, schema: dict | None = None):
        if not credential:
            raise ValueError("API_KEY is required for live mode")
        self.config = config
        self._credential = credential
        self.transport = transport
        self.prompt = prompt
        self.schema = SCHEMA if schema is None else schema
        self.response_source = f"{config.provider}/{config.model}"

    @classmethod
    def from_environment(cls, *, environment: Mapping[str, str] | None = None, transport: Callable = http_transport):
        env = os.environ if environment is None else environment
        if not env.get("MODEL_ENDPOINT") or not env.get("MODEL_ID"):
            raise ValueError("Live mode requires MODEL_ENDPOINT and MODEL_ID")
        config = HttpConfig(
            endpoint=env["MODEL_ENDPOINT"], model=env["MODEL_ID"],
            provider=env.get("MODEL_PROVIDER", "openai-compatible"),
            temperature=float(env.get("MODEL_TEMPERATURE", "0")),
            max_completion_tokens=int(env.get("MODEL_MAX_COMPLETION_TOKENS", "2048")),
            timeout=float(env.get("MODEL_TIMEOUT_SECONDS", "60")),
            max_response_bytes=int(env.get("MODEL_MAX_RESPONSE_BYTES", "1048576")),
        )
        return cls(config, credential=env.get("API_KEY", ""), transport=transport)

    def request_body(self, message: str) -> dict[str, Any]:
        return {
            "model": self.config.model, "messages": build_messages(message, self.prompt),
            "temperature": self.config.temperature, "max_completion_tokens": self.config.max_completion_tokens,
            "stream": False, "response_format": {"type": "json_schema", "json_schema": {
                "name": "access_request", "strict": True, "schema": self.schema}},
        }

    def processing_configuration(self) -> dict[str, Any]:
        config = asdict(self.config)
        return {
            "provider": config.pop("provider"), "model": config.pop("model"),
            "endpoint": config.pop("endpoint"), "request_settings": config,
            "prompt": self.prompt, "schema": self.schema,
        }

    @classmethod
    def from_settings(cls, settings, *, environment: Mapping[str, str] | None = None,
                      transport: Callable = http_transport):
        """Reconstruct a saved producing configuration; load credentials afresh."""
        env = os.environ if environment is None else environment
        config = HttpConfig(endpoint=settings.endpoint, model=settings.model,
                            provider=settings.provider, **settings.request_settings)
        return cls(config, credential=env.get("API_KEY", ""), transport=transport,
                   prompt=settings.prompt, schema=settings.schema)

    def request_evidence(self, message: str) -> dict[str, Any]:
        return {
            "request": {"method": "POST", "endpoint": self.config.endpoint,
                        "headers": {"Content-Type": "application/json", "Authorization": "[REDACTED]"},
                        "body": self.request_body(message)},
            "response_handling": ["HTTP status classification", "bounded UTF-8 JSON envelope",
                                  "refusal before content", "finish_reason must be stop",
                                  "local schema/date/quote validation before routing"],
        }

    def extract(self, message: str) -> dict[str, Any]:
        try:
            reply = self.transport(
                endpoint=self.config.endpoint,
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {self._credential}"},
                body=json.dumps(self.request_body(message)).encode("utf-8"),
                timeout=self.config.timeout, max_response_bytes=self.config.max_response_bytes,
            )
        except ProviderFailure as error:
            raise ProviderFailure(error.code, str(error).replace(self._credential, "[REDACTED]"),
                                  retryable=error.retryable, retry_after=error.retry_after) from None
        except (ProviderUnavailable, OSError, URLError, TimeoutError):
            # Never retain the original exception text or credential-bearing request.
            raise ProviderFailure("provider-unavailable", "Provider transport unavailable; retry extraction", retryable=True) from None
        if reply.status != 200:
            raise ProviderFailure(f"http-{reply.status}", f"Provider returned HTTP {reply.status}; inspect configuration or retry",
                                  retryable=reply.status == 429 or 500 <= reply.status <= 599,
                                  retry_after=_retry_after(reply.headers))
        if len(reply.body) > self.config.max_response_bytes:
            raise ProviderFailure("response-too-large", "Provider response exceeded configured size limit")
        try:
            # Redact an echoed credential before any output can reach state/logs.
            data = json.loads(reply.body.decode("utf-8").replace(self._credential, "[REDACTED]"))
            choices = data["choices"]
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError("Expected one choice")
            choice = choices[0]
            response = choice["message"]
            if not isinstance(response, dict) or "finish_reason" not in choice:
                raise ValueError("Invalid completion envelope")
            refusal = response.get("refusal")
            content = response.get("content")
            reason = choice["finish_reason"]
            if not refusal and reason == "stop" and not isinstance(content, str):
                raise ValueError("Missing content")
            actual_model = data.get("model", self.config.model)
            if not isinstance(actual_model, str) or not actual_model:
                raise ValueError("Invalid model ID")
        except (ValueError, KeyError, TypeError, IndexError, RecursionError):
            raise ProviderFailure("invalid-envelope", "Provider returned an invalid chat-completions envelope") from None
        return {"status": "completed" if reason == "stop" else "incomplete", "refusal": refusal,
                "output_text": content, "response_source": f"{self.config.provider}/{actual_model}"}
