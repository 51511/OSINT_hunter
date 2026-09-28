"""用 Streamlit AppTest 實際渲染 UI，驗證表單與結果顯示不會崩潰。"""
from pathlib import Path

from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parent.parent / "app.py")

from osint_hunter.models import Candidate, Evidence, Investigation, Page, RoundLog, SearchHit, Target


def _fake_inv():
    inv = Investigation(Target("小明", "台北"), model="fake:x")
    inv.hits["a"] = SearchHit("t", "https://ptt.cc/a", "s")
    inv.pages["https://ptt.cc/a"] = Page("https://ptt.cc/a", "標題", "內文", True)
    inv.pages["https://ig.com/x"] = Page("https://ig.com/x", "IG", "摘要", False, "snippet-only")
    inv.candidates = [
        Candidate("ptt", "xmdesign", confidence="high", reasoning="吻合",
                  evidence=[Evidence("https://ptt.cc/a", "我住在台北", True)]),
        Candidate("instagram", "fake", confidence="low", conflicts="疑慮",
                  evidence=[Evidence("https://ptt.cc/a", "杜撰", False)]),
    ]
    inv.rounds = [RoundLog(1, ["q1", "q2"], 10, 8, 5, 3, ["fan001"])]
    inv.report_md = "## 調查目標\n小明"
    inv.warnings = ["測試警告"]
    return inv


def test_empty_state_renders():
    at = AppTest.from_file(APP, default_timeout=20).run()
    assert not at.exception, at.exception
    assert any("目標" in h.value for h in at.header)


def test_result_rendering():
    at = AppTest.from_file(APP, default_timeout=20)
    at.session_state["inv"] = _fake_inv()
    at.run()
    assert not at.exception, at.exception
    # 4 個指標
    assert len(at.metric) == 4
    assert at.metric[3].value == "2"  # 候選帳號數
    # 警告有顯示
    assert any("測試警告" in w.value for w in at.warning)
    # 3 個 tab 內容渲染 + expander 有候選
    labels = " ".join(e.label for e in at.expander)
    assert "xmdesign" in labels and "信心：高" in labels
    assert "fake" in labels and "信心：低" in labels


def test_submit_without_name_errors():
    at = AppTest.from_file(APP, default_timeout=20).run()
    submit = next(b for b in at.button if "開始調查" in b.label)  # 依 label 找，不依賴順序
    submit.click().run()
    assert any("請輸入目標名稱" in e.value for e in at.error)
