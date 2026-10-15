"""LLM 抽象層：以假 SDK client 驗證兩種後端的呼叫參數、串流、重試、JSON 修復。"""
import pytest

from osint_hunter.config import Settings
from osint_hunter.llm import LLM, LLMError, extract_json


# ---------- 假 Anthropic client ----------
class _Blk:
    def __init__(self, t): self.type, self.text = "text", t

class _AResp:
    def __init__(self, t): self.content = [_Blk(t)]

class _AStream:
    def __init__(self, parts): self.text_stream = iter(parts)
    def __enter__(self): return self
    def __exit__(self, *a): return False

class FakeAnthropic:
    def __init__(self, outs, fail=0):
        self.outs, self.fail, self.kwargs, self.calls = list(outs), fail, None, 0
        self.messages = self
    def create(self, **kw):
        self.calls += 1; self.kwargs = kw
        if self.fail > 0:
            self.fail -= 1; raise ConnectionError("boom")
        return _AResp(self.outs.pop(0))
    def stream(self, **kw):
        self.kwargs = kw
        return _AStream(["你", "好", "！"])


# ---------- 假 OpenAI client ----------
class _Msg:
    def __init__(self, c): self.content = c
class _Ch:
    def __init__(self, c=None, d=None): self.message = _Msg(c); self.delta = _Msg(d)
class _OResp:
    def __init__(self, t): self.choices = [_Ch(c=t)]

class FakeOpenAI:
    def __init__(self, outs):
        self.outs, self.kwargs = list(outs), None
        self.chat = self; self.completions = self
    def create(self, **kw):
        self.kwargs = kw
        if kw.get("stream"):
            return iter([type("C", (), {"choices": [_Ch(d=p)]})() for p in ["A", "B", None, "C"]])
        return _OResp(self.outs.pop(0))


def mk(provider, client):
    s = Settings(); s.llm_temperature = 0.2
    llm = LLM(s, provider, "m-x"); llm._client = client
    return llm


def test_anthropic_params():
    c = FakeAnthropic(["ok"]); llm = mk("anthropic", c)
    assert llm.complete("SYS", "USER", max_tokens=99) == "ok"
    assert c.kwargs["system"] == "SYS" and c.kwargs["max_tokens"] == 99
    assert c.kwargs["messages"] == [{"role": "user", "content": "USER"}]
    assert c.kwargs["model"] == "m-x"


def test_anthropic_stream_calls_on_token():
    c = FakeAnthropic([]); llm = mk("anthropic", c); got = []
    llm.on_token = got.append
    assert llm.complete("s", "u", stream=True) == "你好！" and got == ["你", "好", "！"]


def test_openai_params_and_stream():
    c = FakeOpenAI(["hi"]); llm = mk("openai", c)
    assert llm.complete("SYS", "U") == "hi"
    assert c.kwargs["messages"][0] == {"role": "system", "content": "SYS"}
    got = []; llm.on_token = got.append
    assert llm.complete("s", "u", stream=True) == "ABC" and got == ["A", "B", "C"]  # None 片段被略過


def test_retry_then_success(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *_: None)
    c = FakeAnthropic(["fine"], fail=2); llm = mk("anthropic", c)
    assert llm.complete("s", "u") == "fine" and c.calls == 3


def test_retry_exhausted(monkeypatch):
    monkeypatch.setattr("time.sleep", lambda *_: None)
    llm = mk("anthropic", FakeAnthropic(["x"], fail=99))
    with pytest.raises(LLMError, match="連續失敗"):
        llm.complete("s", "u", retries=3)


def test_complete_json_repairs_on_retry():
    c = FakeAnthropic(["這不是 JSON", '```json\n{"queries": ["a",],}\n```']); llm = mk("anthropic", c)
    assert llm.complete_json("s", "u") == {"queries": ["a"]}
    assert c.calls == 2                                  # 第一次壞，第二次修好
    assert "無法解析" in c.kwargs["messages"][0]["content"]   # 錯誤有回饋給模型


def test_complete_json_gives_up():
    llm = mk("anthropic", FakeAnthropic(["爛", "爛", "爛"]))
    with pytest.raises(LLMError, match="合法 JSON"):
        llm.complete_json("s", "u", retries=2)


def test_missing_keys_raise_clear_errors():
    s = Settings(); s.anthropic_api_key = ""; s.openai_api_key = ""; s.openai_base_url = ""
    with pytest.raises(LLMError, match="ANTHROPIC_API_KEY"):
        LLM(s, "anthropic")._get_client()
    with pytest.raises(LLMError, match="OPENAI_API_KEY"):
        LLM(s, "openai")._get_client()
    with pytest.raises(LLMError, match="不支援"):
        LLM(s, "nope")._get_client()


def test_ollama_needs_no_key():
    s = Settings(); s.openai_api_key = ""
    assert LLM(s, "ollama")._get_client() is not None


def test_extract_json_edge():
    assert extract_json('文字 {"a": "含}括號"} 更多') == {"a": "含}括號"}
    with pytest.raises(ValueError):
        extract_json("完全沒有 json")
