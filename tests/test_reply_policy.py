from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.reply_policy import evaluate_reply_policy


def _decision(*, today: int = 1, conversation: int = 1, age_seconds: int | None = None):
    now = datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc)
    last_sent_at = None if age_seconds is None else now - timedelta(seconds=age_seconds)
    return evaluate_reply_policy(
        today_sent=today,
        conversation_sent=conversation,
        last_sent_at=last_sent_at,
        now=now,
        daily_limit=20,
        per_conversation_limit=3,
        interval_seconds=10,
    )


def test_reply_policy_pauses_at_daily_limit() -> None:
    decision = _decision(today=20, conversation=0)

    assert decision.action == "pause"
    assert decision.reason == "今日自动回复已达到上限"


def test_reply_policy_sends_conversation_at_limit_to_manual() -> None:
    decision = _decision(conversation=3)

    assert decision.action == "manual"
    assert decision.reason == "该会话自动回复已达到上限"


def test_reply_policy_waits_for_minimum_interval() -> None:
    decision = _decision(age_seconds=4)

    assert decision.action == "wait"
    assert decision.wait_seconds == 6


def test_reply_policy_allows_after_interval() -> None:
    assert _decision(age_seconds=10).action == "allow"
