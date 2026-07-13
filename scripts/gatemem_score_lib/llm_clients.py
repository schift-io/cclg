"""OpenAI-compatible chat clients for the two approved models only:

- schift-local-a3b (answer generation): a generic OpenAI-compatible
  /chat/completions client configured entirely via env vars --
  base_url=$SCHIFT_LLM_BASE_URL, model=$SCHIFT_LLM_MODEL, Bearer
  $SCHIFT_LLM_API_KEY, chat_template_kwargs.enable_thinking=False
  (Qwen3-style thinking-mode off, ignored by providers that don't
  support it). SCHIFT_LLM_BASE_URL and SCHIFT_LLM_MODEL must both be
  set; there is no built-in default endpoint or model.
- gemini-3.1-flash-lite (judge only): base_url=$GEMINI_API_BASE_URL
  (default Google's OpenAI-compatible endpoint),
  model=$GEMINI_3_1_FLASH_LITE_MODEL, Bearer $GEMINI_API_KEY. Same
  /chat/completions contract as a3b.

No other paid model may be substituted here (absolute guardrail).
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

import requests


class LLMCallError(RuntimeError):
    """Raised for missing credentials or unrecoverable provider errors."""


@dataclass
class LLMUsage:
    input_tokens: int
    output_tokens: int
    total_tokens: int


@dataclass
class LLMResult:
    text: str
    model: str
    usage: LLMUsage
    latency_s: float
    finish_reason: str | None


def _chat_completions_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/openai") or base.endswith("/v1"):
        return f"{base}/chat/completions"
    return f"{base}/v1/chat/completions"


class OpenAICompatibleClient:
    """Minimal client for the shared OpenAI chat-completions contract used by
    both approved models (see module docstring)."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None,
        max_tokens: int,
        temperature: float = 0.0,
        timeout_s: float = 120.0,
        disable_thinking: bool = False,
    ) -> None:
        self.url = _chat_completions_url(base_url)
        self.model = model
        self.api_key = api_key
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout_s = timeout_s
        self.disable_thinking = disable_thinking

    def complete(self, *, system_prompt: str, user_prompt: str, json_mode: bool = True) -> LLMResult:
        messages: list[dict[str, str]] = []
        if system_prompt and system_prompt.strip():
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": user_prompt})

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "stream": False,
        }
        if self.disable_thinking:
            payload["chat_template_kwargs"] = {"enable_thinking": False}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        started = time.monotonic()
        resp = None
        last_err: str | None = None
        for attempt in range(2):
            try:
                resp = requests.post(self.url, json=payload, headers=headers, timeout=self.timeout_s)
            except requests.RequestException as exc:
                last_err = f"network_error: {exc}"
                time.sleep(1.5 * (attempt + 1))
                continue
            if resp.status_code in (429, 500, 502, 503, 504):
                last_err = f"http_{resp.status_code}: {resp.text[:300]}"
                time.sleep(1.5 * (attempt + 1))
                continue
            break
        else:
            raise LLMCallError(f"{self.model} request failed after retries: {last_err}")

        if resp is None:
            raise LLMCallError(f"{self.model} request failed: {last_err}")
        if resp.status_code >= 400:
            raise LLMCallError(f"{self.model} API error {resp.status_code}: {resp.text[:500]}")

        latency_s = time.monotonic() - started
        body = resp.json()
        choice = (body.get("choices") or [{}])[0]
        content = (choice.get("message") or {}).get("content") or ""
        finish_reason = choice.get("finish_reason")
        usage = body.get("usage") or {}
        u = LLMUsage(
            input_tokens=int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
            total_tokens=int(
                usage.get("total_tokens")
                or (int(usage.get("prompt_tokens") or 0) + int(usage.get("completion_tokens") or 0))
            ),
        )
        return LLMResult(text=content, model=self.model, usage=u, latency_s=latency_s, finish_reason=finish_reason)


def schift_a3b_client(*, max_tokens: int = 512, temperature: float = 0.0) -> OpenAICompatibleClient:
    """schift-local-a3b generation client. Raises LLMCallError if any of
    SCHIFT_LLM_BASE_URL, SCHIFT_LLM_MODEL, or SCHIFT_LLM_API_KEY is unset."""
    base_url = os.getenv("SCHIFT_LLM_BASE_URL")
    model = os.getenv("SCHIFT_LLM_MODEL")
    api_key = os.getenv("SCHIFT_LLM_API_KEY")
    missing = [
        name
        for name, value in (
            ("SCHIFT_LLM_BASE_URL", base_url),
            ("SCHIFT_LLM_MODEL", model),
            ("SCHIFT_LLM_API_KEY", api_key),
        )
        if not value
    ]
    if missing:
        raise LLMCallError(
            f"{', '.join(missing)} not set. Export SCHIFT_LLM_API_KEY (and "
            "SCHIFT_LLM_BASE_URL, SCHIFT_LLM_MODEL) before running the "
            "generate/all stages."
        )
    return OpenAICompatibleClient(
        base_url=base_url,
        model=model,
        api_key=api_key,
        max_tokens=max_tokens,
        temperature=temperature,
        disable_thinking=True,
    )


def gemini_judge_client(*, max_tokens: int = 256, temperature: float = 0.0) -> OpenAICompatibleClient:
    """gemini-3.1-flash-lite judge client. Raises LLMCallError if GEMINI_API_KEY unset."""
    base_url = os.getenv("GEMINI_API_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai")
    model = os.getenv("GEMINI_3_1_FLASH_LITE_MODEL", "gemini-3.1-flash-lite")
    api_key = os.getenv("GEMINI_API_KEY")
    if not api_key:
        raise LLMCallError("GEMINI_API_KEY is not set (required for the gemini-3.1-flash-lite judge).")
    return OpenAICompatibleClient(
        base_url=base_url,
        model=model,
        api_key=api_key,
        max_tokens=max_tokens,
        temperature=temperature,
        disable_thinking=False,
    )


class A3BRouterAdapter:
    """Adapts OpenAICompatibleClient to the `llm_router` protocol expected by
    GateMem's bench.agents.utils_llm.generate_llm_output / BaseMemoryAgent._run_llm
    (needs .provider != "stub" and .complete_result(system_prompt=, user_prompt=))."""

    provider = "schift-local-a3b"

    def __init__(self, client: OpenAICompatibleClient) -> None:
        self._client = client
        self.model = client.model

    def complete_result(self, *, system_prompt: str, user_prompt: str, **_ignored: Any):
        res = self._client.complete(system_prompt=system_prompt, user_prompt=user_prompt, json_mode=True)
        return SimpleNamespace(
            text=res.text,
            provider=self.provider,
            model=res.model,
            latency_s=res.latency_s,
            usage={
                "input_tokens": res.usage.input_tokens,
                "output_tokens": res.usage.output_tokens,
                "total_tokens": res.usage.total_tokens,
            },
            raw=None,
        )


class GeminiJudgeRouterAdapter:
    """Adapts OpenAICompatibleClient to the `judge_router` protocol expected by
    GateMem's bench.eval.judge.run_llm_judge (needs .provider, .config.model,
    and .complete_result(system_prompt=, user_prompt=, json_schema=, json_schema_name=))."""

    provider = "gemini-3.1-flash-lite"

    def __init__(self, client: OpenAICompatibleClient) -> None:
        self._client = client
        self.config = SimpleNamespace(model=client.model)

    def complete_result(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        json_schema: dict | None = None,
        json_schema_name: str | None = None,
        **_ignored: Any,
    ):
        res = self._client.complete(
            system_prompt=system_prompt, user_prompt=user_prompt, json_mode=bool(json_schema)
        )
        return SimpleNamespace(
            text=res.text,
            provider=self.provider,
            model=res.model,
            latency_s=res.latency_s,
            usage={
                "input_tokens": res.usage.input_tokens,
                "output_tokens": res.usage.output_tokens,
                "total_tokens": res.usage.total_tokens,
            },
            raw=None,
        )
