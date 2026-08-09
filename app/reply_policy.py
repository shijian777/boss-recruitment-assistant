from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class ReplyPolicyDecision:
    action: str
    reason: str
    wait_seconds: int = 0


def evaluate_reply_policy(
    *,
    today_sent: int,
    conversation_sent: int,
    last_sent_at: datetime | None,
    now: datetime,
    daily_limit: int,
    per_conversation_limit: int,
    interval_seconds: int,
) -> ReplyPolicyDecision:
    """Apply persisted automatic-reply limits in strict priority order."""
    if today_sent >= daily_limit:
        return ReplyPolicyDecision("pause", "今日自动回复已达到上限")
    if conversation_sent >= per_conversation_limit:
        return ReplyPolicyDecision("manual", "该会话自动回复已达到上限")
    if last_sent_at is not None:
        elapsed = max(0, int((now - last_sent_at).total_seconds()))
        remaining = interval_seconds - elapsed
        if remaining > 0:
            return ReplyPolicyDecision("wait", "等待自动回复间隔", remaining)
    return ReplyPolicyDecision("allow", "允许回复")
