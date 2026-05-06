"""Tests for OpenAI narrative generation (E3).

We mock the OpenAI client so tests don't hit the live API. The model
selection, prompt structure, and secret-stripping are exercised
directly.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from symverify import narrative


SAMPLE_SNAPSHOT = {
    "status": "drift",
    "trinity_r": "T9K2-MQ4N-XR8P",
    "trinity_p": "B7H4-NK2X-YR5Q",
    "system_fold": "SYM-7K2P-MQXN-4R8T-9LWZ",
    "brackets": {
        "Q_canonical": {"value": 0, "status": "conserved"},
        "Q_trinity_R": {
            "value": "local≠git",
            "status": "drift_expected",
            "descriptor": "local≠git: 3 uncommitted files",
        },
    },
}


def _mock_client(reply_text: str) -> MagicMock:
    """Return a mock that mimics openai.OpenAI() chat.completions.create."""
    msg = MagicMock()
    msg.content = reply_text
    choice = MagicMock()
    choice.message = msg
    resp = MagicMock()
    resp.choices = [choice]
    client = MagicMock()
    client.chat.completions.create.return_value = resp
    return client


def test_narrate_returns_text_from_mock_client():
    client = _mock_client("All conserved; one expected drift on Q_trinity_R.")
    out = narrative.narrate(SAMPLE_SNAPSHOT, "manual", client=client)
    assert out is not None
    assert "drift" in out.lower() or "conserv" in out.lower()


def test_narrate_calls_openai_with_expected_params():
    client = _mock_client("ok")
    narrative.narrate(SAMPLE_SNAPSHOT, "manual", client=client)
    client.chat.completions.create.assert_called_once()
    kwargs = client.chat.completions.create.call_args.kwargs
    assert kwargs["model"] == narrative.OPENAI_MODEL
    assert kwargs["max_tokens"] == narrative.OPENAI_MAX_TOKENS
    assert kwargs["temperature"] == narrative.OPENAI_TEMPERATURE
    msgs = kwargs["messages"]
    assert msgs[0]["role"] == "system"
    assert msgs[1]["role"] == "user"
    assert "Q_canonical" in msgs[1]["content"]
    assert "Q_trinity_R" in msgs[1]["content"]


def test_narrate_returns_none_when_no_api_key(monkeypatch, tmp_path):
    """Without a key in env or secrets/openai.key, narrate returns None."""
    monkeypatch.setenv("SYMVERIFY_HOME", str(tmp_path))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    out = narrative.narrate(SAMPLE_SNAPSHOT, "manual")
    assert out is None


def test_narrate_uses_env_api_key(monkeypatch):
    """If OPENAI_API_KEY is set in env, narrate should attempt the call.
    We verify by passing an explicit client (which short-circuits key lookup)."""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-fake")
    client = _mock_client("test reply")
    out = narrative.narrate(SAMPLE_SNAPSHOT, "manual", client=client)
    assert out == "test reply"


def test_narrate_returns_none_on_client_exception():
    client = MagicMock()
    client.chat.completions.create.side_effect = RuntimeError("API down")
    out = narrative.narrate(SAMPLE_SNAPSHOT, "manual", client=client)
    assert out is None


def test_narrate_returns_none_on_empty_reply():
    client = _mock_client("")
    out = narrative.narrate(SAMPLE_SNAPSHOT, "manual", client=client)
    assert out is None


# Test fixtures split so the source bytes don't themselves match Q_secrets
# regexes (which would alarm on every audit of this repo).
_AWS_PREFIX = "AKIA"
_AWS_BODY = "IOSFODNN7" + "EXAMPLE"
_GH_PREFIX = "ghp_"
_GH_BODY = "1234567890" + "ABCDEFabcdef" + "1234567890" + "ABCD"


def test_strip_secrets_redacts_aws_key():
    fake = _AWS_PREFIX + _AWS_BODY
    s = f"details: {fake} was found"
    stripped = narrative._strip_secrets(s)
    assert fake not in stripped
    assert "REDACTED" in stripped


def test_strip_secrets_redacts_github_pat():
    fake = _GH_PREFIX + _GH_BODY
    s = f"GH_TOKEN={fake} please fix"
    stripped = narrative._strip_secrets(s)
    assert fake not in stripped


def test_unavailable_text_format():
    assert narrative.unavailable_text("rate limit") == "<narrative unavailable: rate limit>"


def test_user_prompt_includes_trigger_and_brackets():
    """Direct unit test of the prompt builder."""
    out = narrative._build_user_prompt(SAMPLE_SNAPSHOT, "drift_detected")
    assert "drift_detected" in out
    assert "Q_canonical" in out
    assert "Q_trinity_R" in out
    assert "T9K2-MQ4N-XR8P" in out


def test_user_prompt_strips_secrets():
    fake = _GH_PREFIX + "abcd" + _GH_BODY[:32]  # 36 chars after ghp_
    snap = dict(SAMPLE_SNAPSHOT)
    snap["brackets"] = dict(snap["brackets"])
    snap["brackets"]["Q_secrets"] = {
        "value": 1,
        "status": "drift_alarm",
        "descriptor": f"matched {fake}",
    }
    out = narrative._build_user_prompt(snap, "drift_detected")
    assert fake not in out
