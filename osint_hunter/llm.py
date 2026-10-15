"""LLM 抽象層：Anthropic / OpenAI 相容（OpenAI、Ollama、Groq、LM Studio…）。

只暴露兩件事：
  - LLM.complete(system, user)      → str
  - LLM.complete_json(system, user) → 已解析的 Python 物件（含容錯修復）
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Any, Callable

from .config import Settings

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    pass


def extract_json(text: str) -> Any:
    """從 LLM 輸出中抽出 JSON（容忍 code fence、前後贅字、尾逗號、單引號）。"""
    if text is None:
        raise ValueError("empty")
    s = text.strip()
    s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
    s = re.sub(r"\s*```$", "", s).strip()

    # 直接解析
    try:
        return json.loads(s)
    except Exception:
        pass

    # 找第一個完整的 {...} 或 [...]
    for opener, closer in (("{", "}"), ("[", "]")):
        start = s.find(opener)
        if start == -1:
            continue
        depth, in_str, esc = 0, False, False
        for i in range(start, len(s)):
            ch = s[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    chunk = s[start : i + 1]
                    for candidate in (chunk, re.sub(r",\s*([}\]])", r"\1", chunk)):
                        try:
                            return json.loads(candidate)
                        except Exception:
                            continue
                    break
    raise ValueError(f"無法從輸出解析 JSON：{text[:200]!r}")


class LLM:
    def __init__(self, settings: Settings, provider: str | None = None, model: str | None = None):
        self.s = settings
        self.provider = (provider or settings.default_provider).lower()
        self.model = model or settings.default_model
        self._client: Any = None
        self.on_token: Callable[[str], None] | None = None  # 串流回呼（給 UI 用）

    # ---------- client ----------
    def _get_client(self):
        if self._client is not None:
            return self._client
        if self.provider == "anthropic":
            if not self.s.anthropic_api_key:
                raise LLMError("未設定 ANTHROPIC_API_KEY")
            import anthropic
            self._client = anthropic.Anthropic(api_key=self.s.anthropic_api_key)
        elif self.provider in ("openai", "ollama", "custom"):
            import openai
            if self.provider == "ollama":
                self._client = openai.OpenAI(base_url=self.s.ollama_base_url, api_key="ollama")
            else:
                if not self.s.openai_api_key and not self.s.openai_base_url:
                    raise LLMError("未設定 OPENAI_API_KEY（或 OPENAI_BASE_URL）")
                kwargs: dict[str, Any] = {"api_key": self.s.openai_api_key or "none"}
                if self.s.openai_base_url:
                    kwargs["base_url"] = self.s.openai_base_url
                self._client = openai.OpenAI(**kwargs)
        else:
            raise LLMError(f"不支援的 provider：{self.provider}")
        return self._client

    # ---------- completion ----------
    def complete(self, system: str, user: str, *, stream: bool = False,
                 max_tokens: int | None = None, retries: int = 3) -> str:
        client = self._get_client()
        max_tokens = max_tokens or self.s.llm_max_tokens
        last: Exception | None = None
        for attempt in range(retries):
            try:
                if self.provider == "anthropic":
                    return self._anthropic(client, system, user, stream, max_tokens)
                return self._openai(client, system, user, stream, max_tokens)
            except LLMError:
                raise
            except Exception as e:  # 網路 / 限流 / 5xx
                last = e
                wait = 2 ** attempt
                log.warning("LLM 呼叫失敗（第 %d 次）：%s；%ss 後重試", attempt + 1, e, wait)
                time.sleep(wait)
        raise LLMError(f"LLM 呼叫連續失敗：{last}")

    def _anthropic(self, client, system, user, stream, max_tokens) -> str:
        kwargs = dict(
            model=self.model,
            max_tokens=max_tokens,
            temperature=self.s.llm_temperature,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        if stream and self.on_token:
            out: list[str] = []
            with client.messages.stream(**kwargs) as st:
                for chunk in st.text_stream:
                    out.append(chunk)
                    self.on_token(chunk)
            return "".join(out)
        resp = client.messages.create(**kwargs)
        return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")

    def _openai(self, client, system, user, stream, max_tokens) -> str:
        kwargs = dict(
            model=self.model,
            temperature=self.s.llm_temperature,
            max_tokens=max_tokens,
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
        )
        if stream and self.on_token:
            out: list[str] = []
            for chunk in client.chat.completions.create(stream=True, **kwargs):
                if not chunk.choices:
                    continue
                piece = chunk.choices[0].delta.content or ""
                if piece:
                    out.append(piece)
                    self.on_token(piece)
            return "".join(out)
        resp = client.chat.completions.create(**kwargs)
        return resp.choices[0].message.content or ""

    def complete_json(self, system: str, user: str, *, max_tokens: int | None = None,
                      retries: int = 2) -> Any:
        """要求 JSON 輸出；解析失敗時把錯誤回饋給模型重試。"""
        sys_j = system.rstrip() + "\n\n【輸出格式】只輸出合法 JSON，不要任何說明文字或 code fence。"
        prompt = user
        last_err = ""
        for _ in range(retries + 1):
            raw = self.complete(sys_j, prompt, max_tokens=max_tokens)
            try:
                return extract_json(raw)
            except ValueError as e:
                last_err = str(e)
                prompt = (
                    user
                    + f"\n\n（上一次輸出無法解析為 JSON：{last_err}。請只輸出合法 JSON。）"
                )
        raise LLMError(f"LLM 連續 {retries + 1} 次未輸出合法 JSON：{last_err}")
