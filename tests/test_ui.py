"""The Streamlit UI, driven offline by Streamlit's AppTest over the committed data.

Chat answers here come from a stand-in for ChatAgent.ask (test-only), so the page's
rendering, streaming and error handling are checked without an API call. The real agent is
exercised by the eval."""

import anthropic
import pytest
from streamlit.testing.v1 import AppTest

from leadership.chat.agent import ChatAgent, Reply, Step
from leadership.config import REPO_ROOT

APP = str(REPO_ROOT / "src" / "leadership" / "ui" / "app.py")


def _app() -> AppTest:
    return AppTest.from_file(APP, default_timeout=60)


def _pick(at: AppTest, domain: str) -> AppTest:
    box = at.selectbox(key="company")
    box.select_index(next(i for i, o in enumerate(box.options) if domain in o)).run()
    return at


@pytest.fixture
def no_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-used")


def test_missing_key_is_a_friendly_message_and_the_table_still_works(no_key):
    at = _pick(_app().run(), "robinhood.com")
    assert not at.exception
    assert any("ANTHROPIC_API_KEY" in i.value for i in at.info)
    assert at.chat_input[0].disabled
    suggested = [b for b in at.button if b.key and b.key.startswith("suggest:")]
    assert {b.label for b in suggested} == {
        "Who's their CTO?", "How many VPs do they have?", "Who heads marketing?",
        "Where is their CEO based?",
    }  # fmt: skip
    assert all(b.disabled for b in suggested)
    # Leadership tab: grouped tables (C-level, VP, Head) covering all 22 current people.
    assert sum(len(d.value) for d in at.dataframe) == 22
    assert {"Name", "Title", "Function", "Seniority", "Location", "Confidence", "Sources"} <= set(
        at.dataframe[0].value.columns
    )
    assert any("Koegel" in w.value and "Klarna" in w.value for w in at.warning)


def test_sidebar_shows_last_run_stats(no_key):
    at = _pick(_app().run(), "meetcampfire.com")
    metrics = {m.label: m.value for m in at.sidebar.metric}
    assert metrics == {"People found": "9", "Current": "8"}
    captions = " ".join(c.value for c in at.sidebar.caption)
    assert "Company site: 88" in captions and "Extraction cost: $0.67" in captions
    assert len([b for b in at.button if b.key and b.key.startswith("suggest:")]) == 5


def test_not_current_toggle_lists_people_with_their_reason(no_key):
    at = _pick(_app().run(), "robinhood.com")
    before = len(at.dataframe)
    at.toggle(key="lead_former").set_value(True).run()
    assert len(at.dataframe) == before + 1
    former = at.dataframe[-1].value
    pinner = former[former["Name"] == "Jeff Pinner"].iloc[0]
    assert pinner["Last confirmed"] == "2026-05-08" and "8-K" in pinner["Why not current"]


def test_filters_narrow_the_table(no_key):
    at = _pick(_app().run(), "robinhood.com")
    at.multiselect(key="lead_conf").set_value(["high"]).run()
    assert sum(len(d.value) for d in at.dataframe) == 7


def _fake_ask(self, question, on_event=None):
    step = Step("list_leaders", {"company": "robinhood.com", "function": "finance"}, False,
                '{"current": [{"sources": [{"url": "https://example.com/proxy", '
                '"source_type": "sec_filing", "published": "2026-04-22", '
                '"evidence": "Shiv Verma, Chief Financial Officer"}]}]}')  # fmt: skip
    events = [
        ("round", 0),
        ("text", "Let me check."),
        ("tool", step),
        ("round", 1),  # fmt: skip
        ("text", "Shiv Verma is the CFO "),
        ("text", "(https://example.com/proxy)."),
    ]
    for kind, data in events:
        on_event(kind, data)
    return Reply(
        "Shiv Verma is the CFO (https://example.com/proxy).", [step],
        ["https://example.com/proxy"],
        [{"url": "https://example.com/proxy", "source_type": "sec_filing",
          "published": "2026-04-22", "evidence": ["Shiv Verma, Chief Financial Officer"]}],
        input_tokens=100, cache_read_tokens=900, output_tokens=50, cost_usd=0.0021,
    )  # fmt: skip


def test_suggested_question_answer_shows_sources_steps_and_cost(key, monkeypatch):
    monkeypatch.setattr(ChatAgent, "ask", _fake_ask)
    at = _pick(_app().run(), "robinhood.com")
    at.button(key="suggest:robinhood.com:0").click().run()
    assert not at.exception
    assert any("Shiv Verma is the CFO" in m.value for m in at.markdown)
    labels = [e.label for e in at.expander]
    assert "Sources (1)" in labels and "Agent steps (1)" in labels
    assert any("Shiv Verma, Chief Financial Officer" in t.value for t in at.text)
    assert any('list_leaders(company="robinhood.com", function="finance")' in c.value
               for c in at.code)  # fmt: skip
    assert any("$0.0021" in c.value for c in at.caption)
    assert not [b for b in at.button if b.key and b.key.startswith("suggest:")]  # chat not empty


@pytest.mark.parametrize(
    ("exc", "text"),
    [
        (anthropic.APIConnectionError(request=None),
         "Couldn't reach the Anthropic API"),
        (RuntimeError("ANTHROPIC_API_KEY was rejected (HTTP 401)"), "was rejected"),
        (ZeroDivisionError("boom"), "Something went wrong"),
    ],
)  # fmt: skip
def test_api_failures_are_friendly_messages_not_tracebacks(key, monkeypatch, exc, text):
    def boom(self, question, on_event=None):
        raise exc

    monkeypatch.setattr(ChatAgent, "ask", boom)
    at = _pick(_app().run(), "meetcampfire.com")
    at.chat_input(key="input:meetcampfire.com").set_value("Who's the CFO?").run()
    assert not at.exception
    assert any(text in e.value for e in at.error)
    assert not any("Traceback" in m.value or "boom" in m.value for m in at.markdown)


def test_ui_launch_is_headless_everywhere():
    """No first-run email prompt: the CLI, the VS Code config and config.toml all run headless
    and send no usage stats."""
    import json
    import tomllib

    from leadership.cli import ui_command

    cmd = ui_command(8501)
    flags = dict(zip(cmd[5::2], cmd[6::2], strict=True))
    assert cmd[1:4] == ["-m", "streamlit", "run"] and cmd[4].endswith("app.py")
    assert flags == {"--server.port": "8501", "--server.headless": "true",
                     "--browser.gatherUsageStats": "false"}  # fmt: skip
    launch = json.loads((REPO_ROOT / ".vscode" / "launch.json").read_text(encoding="utf-8"))
    (ui,) = [c for c in launch["configurations"] if c["name"] == "Launch UI"]
    args = ui["args"]
    assert args[args.index("--server.headless") + 1] == "true"
    assert args[args.index("--browser.gatherUsageStats") + 1] == "false"
    config = tomllib.loads((REPO_ROOT / ".streamlit" / "config.toml").read_text(encoding="utf-8"))
    assert config["server"]["headless"] is True
    assert config["server"]["address"] == "localhost"  # this machine only
    assert config["browser"]["gatherUsageStats"] is False
