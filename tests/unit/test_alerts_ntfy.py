"""titan2.alerts.Ntfy: push notifications through ntfy (the user's phone; Telegram is not an option in India)."""

from __future__ import annotations

import types

import pytest

from titan2 import alerts
from titan2.alerts import AlertError, Fanout, Ntfy

TOPIC = "titan2-example-not-a-real-topic"


class Post:
    def __init__(self, status: int = 200, exc: Exception | None = None) -> None:
        self.status, self.exc, self.calls = status, exc, []

    def __call__(self, url, data=None, headers=None, timeout=None):
        self.calls.append((url, data, headers, timeout))
        if self.exc is not None:
            raise self.exc
        return types.SimpleNamespace(status_code=self.status)


def test_from_env_and_topic_rules():
    assert Ntfy.from_env({}) is None
    assert repr(Ntfy.from_env({"NTFY_TOPIC": TOPIC})) == "Ntfy(<configured>)"
    for bad in ("short", "has space in it 12345678", "x" * 65, "slash/inside-0123456789"):
        with pytest.raises(ValueError):
            Ntfy(bad)
    with pytest.raises(ValueError):
        Ntfy(TOPIC, "http://ntfy.example.com")                       # https only


def test_send_maps_severity_to_priority_and_title():
    post = Post()
    n = Ntfy(TOPIC, post=post)
    n.send(alerts.format_alert("CRITICAL", "UNKNOWN_POSITION", "a position titan2 does not own"))
    n.send(alerts.format_alert("WARN", "HALT_FLATTEN", "operator FLATTEN_ALL"))
    n.send(alerts.format_alert("INFO", "POSITION_CLOSED", "PAPER:price_spike_up NFO:X closed: net Rs 746.71"))
    (u1, d1, h1, _), (u2, _, h2, _), (u3, d3, h3, _) = post.calls
    assert u1 == f"https://ntfy.sh/{TOPIC}" and u2 == u1 and u3 == u1
    assert (h1["Priority"], h1["Tags"]) == ("5", "rotating_light") and h1["Title"].startswith("titan2 CRITICAL UNKNOWN_POSITION")
    assert (h2["Priority"], h3["Priority"]) == ("4", "3")
    assert b"net Rs 746.71" in d3 and all(ord(ch) < 128 for ch in h3["Title"])


def test_errors_never_show_the_topic():
    n = Ntfy(TOPIC, post=Post(exc=ConnectionError(f"HTTPSConnectionPool(host='ntfy.sh'): /{TOPIC} failed")))
    with pytest.raises(AlertError) as e:
        n.send("[titan2] WARN X\nboom")
    assert TOPIC not in str(e.value)
    with pytest.raises(AlertError, match="ntfy: HTTP 429"):
        Ntfy(TOPIC, post=Post(status=429)).send("[titan2] INFO Y\nz")


def test_fanout_and_message_channel(caplog):
    ok, bad = Post(), Post(exc=OSError("down"))
    Fanout([Ntfy(TOPIC, post=bad), Ntfy(TOPIC + "x", post=ok)]).send("[titan2] INFO A\nb")   # one delivered: fine
    assert len(ok.calls) == 1 and "another delivered it" in caplog.text
    with pytest.raises(AlertError):
        Fanout([Ntfy(TOPIC, post=bad)]).send("[titan2] INFO A\nb")
    assert alerts.message_channel({}) is None
    assert isinstance(alerts.message_channel({"NTFY_TOPIC": TOPIC}), Ntfy)
    both = alerts.message_channel({"NTFY_TOPIC": TOPIC, "TELEGRAM_BOT_TOKEN": "123:abc", "TELEGRAM_CHAT_ID": "42"})
    assert isinstance(both, Fanout) and len(both.channels) == 2
    assert alerts.message_channel({"NTFY_TOPIC": "short"}) is None and "that channel is disabled" in caplog.text


def test_send_now_and_the_alerter_use_ntfy(monkeypatch):
    post = Post()
    monkeypatch.setattr(alerts, "_default_post", post)
    assert alerts.send_now("WARN", "TEST", "hello", env={"NTFY_TOPIC": TOPIC}, sleep=lambda s: None) is True
    assert post.calls and post.calls[-1][2]["Priority"] == "4"
    a = alerts.Alerter.from_env({"NTFY_TOPIC": TOPIC})
    assert isinstance(a.telegram, Ntfy)                              # the alerter's message channel
