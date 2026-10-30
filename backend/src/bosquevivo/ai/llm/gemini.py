import re
import time
from typing import Any

from google import genai
from google.genai import errors, types

from bosquevivo.ai.llm.cache import ResponseCache, request_key
from bosquevivo.ai.llm.errors import QuotaExceededError
from bosquevivo.ai.llm.limits import RateLimiter
from bosquevivo.ai.llm.types import LLMResponse, Message, ToolCall, ToolSpec
from bosquevivo.core.errors import ExternalServiceError
from bosquevivo.core.logging import get_logger

log = get_logger(__name__)

RETRYABLE = {429, 500, 502, 503, 504}
MAX_RETRY_DELAY = 60.0
# El 429 de Gemini indica cuánto esperar, p. ej. "retryDelay": "17s"
RETRY_DELAY = re.compile(r"retryDelay['\"]?\s*:\s*['\"]?(\d+(?:\.\d+)?)s")


def retry_delay(exc: Exception, attempt: int) -> float:
    match = RETRY_DELAY.search(f"{getattr(exc, 'details', '')} {exc}")
    delay = float(match.group(1)) if match else float(2**attempt)
    return min(delay, MAX_RETRY_DELAY)


def _to_contents(messages: list[Message]) -> list[Any]:
    contents: list[Any] = []
    for message in messages:
        if message.raw is not None:
            contents.append(message.raw)
            continue
        parts: list[types.Part] = []
        if message.text:
            parts.append(types.Part.from_text(text=message.text))
        for media in message.media:
            parts.append(types.Part.from_bytes(data=media.data, mime_type=media.mime_type))
        for call in message.tool_calls:
            parts.append(types.Part.from_function_call(name=call.name, args=call.args))
        for result in message.tool_results:
            parts.append(
                types.Part.from_function_response(name=result.name, response=result.content)
            )
        role = "model" if message.role == "model" else "user"
        contents.append(types.Content(role=role, parts=parts))
    return contents


def _fingerprint(messages: list[Message]) -> list[dict[str, Any]]:
    return [
        {
            "role": m.role,
            "text": m.text,
            "media": [len(p.data) for p in m.media],
            "calls": [(c.name, c.args) for c in m.tool_calls],
            "results": [(r.name, r.content) for r in m.tool_results],
        }
        for m in messages
    ]


class GeminiProvider:
    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        light_model: str | None = None,
        limiter: RateLimiter | None = None,
        cache: ResponseCache | None = None,
        max_retries: int = 3,
        sleep=time.sleep,
    ) -> None:
        if not api_key or not model:
            raise ExternalServiceError("GEMINI_API_KEY y GEMINI_MODEL son obligatorios")
        self.model = model
        self.light_model = light_model or model
        self.client = genai.Client(api_key=api_key)
        self.limiter = limiter
        self.cache = cache
        self.max_retries = max_retries
        self.sleep = sleep

    def generate(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        json_schema: dict[str, Any] | None = None,
        light: bool = False,
    ) -> LLMResponse:
        model = self.light_model if light else self.model
        key = request_key(model, system, _fingerprint(messages), json_schema)
        if self.cache and not tools:
            hit = self.cache.get(key)
            if hit:
                return hit
        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=0.2,
            max_output_tokens=1024,
        )
        if tools:
            config.tools = [
                types.Tool(
                    function_declarations=[
                        types.FunctionDeclaration(
                            name=t.name,
                            description=t.description,
                            parameters_json_schema=t.parameters,
                        )
                        for t in tools
                    ]
                )
            ]
            config.automatic_function_calling = types.AutomaticFunctionCallingConfig(disable=True)
        if json_schema:
            config.response_mime_type = "application/json"
            config.response_json_schema = json_schema
        response = self._call(model, _to_contents(messages), config)
        parsed = self._parse(response)
        if self.cache and not tools:
            self.cache.put(key, parsed)
        return parsed

    def _call(self, model: str, contents: list[Any], config: types.GenerateContentConfig):
        for attempt in range(self.max_retries + 1):
            if self.limiter:
                self.limiter.acquire()
            try:
                return self.client.models.generate_content(
                    model=model, contents=contents, config=config
                )
            except errors.APIError as exc:
                code = getattr(exc, "code", 0)
                if code in RETRYABLE and attempt < self.max_retries:
                    delay = retry_delay(exc, attempt)
                    log.warning("gemini_retry", code=code, attempt=attempt, delay=delay)
                    self.sleep(delay)
                    continue
                if code == 429:
                    raise QuotaExceededError("cuota del modelo agotada") from exc
                raise ExternalServiceError(f"error del modelo ({code}): {exc}") from exc
        raise ExternalServiceError("reintentos agotados")

    def _parse(self, response) -> LLMResponse:
        usage = getattr(response, "usage_metadata", None)
        prompt_tokens = getattr(usage, "prompt_token_count", 0) or 0
        output_tokens = getattr(usage, "candidates_token_count", 0) or 0
        candidates = getattr(response, "candidates", None) or []
        if not candidates or candidates[0].content is None or not candidates[0].content.parts:
            return LLMResponse(blocked=True, prompt_tokens=prompt_tokens)
        content = candidates[0].content
        calls = [
            ToolCall(p.function_call.name or "", dict(p.function_call.args or {}))
            for p in content.parts or []
            if p.function_call
        ]
        text_parts = [p.text for p in content.parts or [] if p.text and not p.thought]
        text = "".join(text_parts).strip() or None
        return LLMResponse(
            text=text,
            tool_calls=calls,
            raw=content,
            prompt_tokens=prompt_tokens,
            output_tokens=output_tokens,
        )
