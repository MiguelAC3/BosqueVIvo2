from types import SimpleNamespace

import pytest
from google.genai import errors

from bosquevivo.ai.agent.fallback import BUSY, fallback_answer
from bosquevivo.ai.llm import QuotaExceededError
from bosquevivo.ai.llm.gemini import MAX_RETRY_DELAY, GeminiProvider, retry_delay
from bosquevivo.ai.llm.limits import RateLimiter


class Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.now += seconds


def quota_error(delay: str = "17s") -> errors.APIError:
    body = {
        "error": {
            "code": 429,
            "message": "Resource has been exhausted",
            "status": "RESOURCE_EXHAUSTED",
            "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": delay}],
        }
    }
    return errors.APIError(429, body)


def test_limiter_waits_for_a_free_slot_instead_of_failing():
    clock = Clock()
    limiter = RateLimiter(1, 100, clock=clock, max_wait=90, sleep=clock.sleep)
    limiter.acquire()
    limiter.acquire()
    assert clock.now > 60 and limiter.used_today == 2


def test_limiter_gives_up_when_the_wait_is_too_long():
    clock = Clock()
    limiter = RateLimiter(1, 100, clock=clock, max_wait=10, sleep=clock.sleep)
    limiter.acquire()
    with pytest.raises(QuotaExceededError):
        limiter.acquire()
    assert clock.slept == []


def test_limiter_never_waits_for_the_daily_limit():
    clock = Clock()
    limiter = RateLimiter(100, 1, clock=clock, max_wait=3600, sleep=clock.sleep)
    limiter.acquire()
    with pytest.raises(QuotaExceededError):
        limiter.acquire()
    assert clock.slept == []


def test_retry_delay_follows_what_gemini_asks():
    assert retry_delay(quota_error("17s"), attempt=0) == 17.0
    assert retry_delay(quota_error("900s"), attempt=0) == MAX_RETRY_DELAY
    assert retry_delay(errors.APIError(503, {"error": {"message": "x"}}), attempt=2) == 4.0


def test_gemini_retries_quota_errors_with_the_requested_delay():
    slept: list[float] = []
    provider = GeminiProvider("clave-falsa", "modelo", max_retries=2, sleep=slept.append)

    def always_429(**_):
        raise quota_error("21s")

    provider.client = SimpleNamespace(models=SimpleNamespace(generate_content=always_429))
    with pytest.raises(QuotaExceededError):
        provider._call("modelo", [], None)  # type: ignore[arg-type]
    assert slept == [21.0, 21.0]


def test_quota_fallback_says_it_is_busy_not_that_data_is_missing():
    text = fallback_answer([], "comunidad", "cuota")
    assert text.startswith(BUSY)
    assert "datos suficientes" not in text
