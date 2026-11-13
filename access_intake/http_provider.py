"""Configurable chat-completions HTTP adapter with an injected transport."""

import copy
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
    """Controlled diagnostics and retry metadata, without request credentials."""

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
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment):
            raise ValueError("MODEL_ENDPOINT must be HTTPS without credentials, query or fragment")
        if not self.model.strip() or not self.provider.strip():
            raise ValueError("Provider and model ID must be nonempty")
        if (not math.isfinite(self.timeout) or self.timeout <= 0 or self.max_response_bytes <= 0
                or self.max_completion_tokens <= 0):
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


def http_transport(*, endpoint: str, headers: dict[str, str], body: bytes,
                   timeout: float, max_response_bytes: int) -> HttpReply:
    """One bounded HTTPS request; do not follow credential-bearing redirects."""
    request = Request(endpoint, data=body, headers=headers, method="POST")
    try:
        response = build_opener(_NoRedirect()).open(request, timeout=timeout)
    except HTTPError as error:
        response = error
    with response:
        data = response.read(max_response_bytes + 1)
        if len(data) > max_response_bytes:
            raise ProviderFailure("response-too-large", "Provider response exceeded configured size limit")
        return HttpReply(response.code, dict(response.headers.items()), data)


def _retry_after(headers: Mapping[str, str]) -> float | None:
    value = next((value for name, value in headers.items() if name.lower() == "retry-after"), None)
    if value is None:
        return None
    try:
        seconds = float(value)
    except (ValueError, TypeError):
        try:
            seconds = (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0, seconds) if math.isfinite(seconds) else None


class HttpProvider:
    def __init__(self, config: HttpConfig, *, credential: str, transport: Callable = http_transport,
                 prompt: str = SYSTEM_PROMPT, schema: dict | None = None):
        if not credential or any(character in credential for character in "\r\n"):
            raise ValueError("API_KEY must be a nonempty single-line environment credential")
        self.config = config
        self._credential = credential
        self.transport = transport
        self.prompt = prompt
        self.schema = copy.deepcopy(SCHEMA if schema is None else schema)
        self.response_source = f"{config.provider}/{config.model}"

    @classmethod
    def from_environment(cls, *, environment: Mapping[str, str] | None = None, transport: Callable = http_transport):
        env = os.environ if environment is None else environment
        if not env.get("MODEL_ENDPOINT") or not env.get("MODEL_ID"):
            raise ValueError("Live mode requires explicit MODEL_ENDPOINT and MODEL_ID")
        try:
            config = HttpConfig(
                endpoint=env["MODEL_ENDPOINT"], model=env["MODEL_ID"],
                provider=env.get("MODEL_PROVIDER", "openai-compatible"),
                temperature=float(env.get("MODEL_TEMPERATURE", "0")),
                max_completion_tokens=int(env.get("MODEL_MAX_COMPLETION_TOKENS", "2048")),
                timeout=float(env.get("MODEL_TIMEOUT_SECONDS", "60")),
                max_response_bytes=int(env.get("MODEL_MAX_RESPONSE_BYTES", "1048576")),
            )
        except (ValueError, OverflowError):
            raise ValueError("Invalid model configuration; check endpoint, model, temperature and positive limits") from None
        return cls(config, credential=env.get("API_KEY", ""), transport=transport)

    def processing_configuration(self) -> dict[str, Any]:
        """Describe the actual request configuration without credentials."""
        request_settings = asdict(self.config)
        return {
            "provider": request_settings.pop("provider"), "model": request_settings.pop("model"),
            "endpoint": request_settings.pop("endpoint"), "request_settings": request_settings,
            "prompt": self.prompt, "schema": copy.deepcopy(self.schema),
        }

    @classmethod
    def from_settings(cls, settings, *, environment: Mapping[str, str] | None = None,
                      transport: Callable = http_transport):
        """Rebuild saved settings; obtain credentials afresh, never from state."""
        env = os.environ if environment is None else environment
        config = HttpConfig(endpoint=settings.endpoint, model=settings.model,
                            provider=settings.provider, **settings.request_settings)
        return cls(config, credential=env.get("API_KEY", ""), transport=transport,
                   prompt=settings.prompt, schema=settings.schema)

    def request_body(self, message: str) -> dict[str, Any]:
        return {
            "model": self.config.model,
            "messages": build_messages(message, self.prompt),
            "temperature": self.config.temperature,
            "max_completion_tokens": self.config.max_completion_tokens,
            "stream": False,
            "response_format": {"type": "json_schema", "json_schema": {
                "name": "access_request", "strict": True, "schema": copy.deepcopy(self.schema),
            }},
        }

    def _redact(self, value):
        if isinstance(value, str):
            return value.replace(self._credential, "[REDACTED]")
        if isinstance(value, dict):
            return {self._redact(key): self._redact(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        return value

    def request_evidence(self, message: str) -> dict[str, Any]:
        return self._redact({
            "request": {"method": "POST", "endpoint": self.config.endpoint,
                        "headers": {"Content-Type": "application/json", "Authorization": "[REDACTED]"},
                        "body": self.request_body(message)},
            "response_handling": [
                "Classify HTTP status and transport errors; parse Retry-After",
                "Bound response bytes; require UTF-8 JSON and one completion choice",
                "Check refusal before using content; require finish_reason=stop for completion",
                "Validate extraction JSON/schema, dates, null issues and exact source quotes before routing",
                "Publish actionable errors with null extraction; isolate unaffected peers",
            ],
        })

    def extract(self, message: str) -> dict[str, Any]:
        try:
            reply = self.transport(
                endpoint=self.config.endpoint,
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {self._credential}"},
                body=json.dumps(self.request_body(message), ensure_ascii=False, allow_nan=False).encode("utf-8"),
                timeout=self.config.timeout, max_response_bytes=self.config.max_response_bytes,
            )
        except ProviderFailure as error:
            raise ProviderFailure(error.code, self._redact(str(error)), retryable=error.retryable,
                                  retry_after=error.retry_after) from None
        except TimeoutError:
            raise ProviderFailure("provider-timeout", "Provider transport timed out; retry extraction", retryable=True) from None
        except (ProviderUnavailable, OSError, URLError) as error:
            if isinstance(error, URLError) and isinstance(error.reason, TimeoutError):
                raise ProviderFailure("provider-timeout", "Provider transport timed out; retry extraction", retryable=True) from None
            raise ProviderFailure("provider-unavailable", "Provider transport unavailable; retry extraction", retryable=True) from None
        if len(reply.body) > self.config.max_response_bytes:
            raise ProviderFailure("response-too-large", "Provider response exceeded configured size limit")
        if reply.status != 200:
            raise ProviderFailure(
                f"http-{reply.status}", f"Provider returned HTTP {reply.status}; inspect configuration or retry",
                retryable=reply.status == 429 or 500 <= reply.status <= 599,
                retry_after=_retry_after(reply.headers),
            )
        try:
            data = self._redact(json.loads(reply.body.decode("utf-8")))
            choices = data["choices"]
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError("Expected one completion choice")
            choice = choices[0]
            response = choice["message"]
            reason = choice["finish_reason"]
            model = data["model"]
            if (not isinstance(model, str) or not model.strip() or not isinstance(response, dict)
                    or response.get("role") != "assistant"
                    or reason not in ("stop", "length", "content_filter", "tool_calls", "function_call")):
                raise ValueError("Invalid completion envelope")
            refusal = response.get("refusal")
            content = response.get("content")
            if refusal is not None and not isinstance(refusal, str):
                raise ValueError("Invalid refusal indicator")
            if not refusal and reason == "stop" and not isinstance(content, str):
                raise ValueError("Missing completion content")
        except (ValueError, KeyError, TypeError, IndexError, RecursionError):
            raise ProviderFailure("invalid-envelope", "Provider returned an invalid chat-completions envelope") from None
        return {
            "status": "completed" if reason == "stop" else "incomplete",
            "refusal": refusal, "output_text": content,
            "response_source": f"{self.config.provider}/{model}",
        }
