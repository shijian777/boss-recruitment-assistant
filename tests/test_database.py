from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.database import CandidateRecord, Database, ReplyRecord


def _record(key: str, status: str) -> CandidateRecord:
    return CandidateRecord(candidate_key=key, candidate_name="张某", status=status)


def test_database_dedup_and_daily_sent_count(tmp_path) -> None:
    database = Database(tmp_path / "records.db")
    database.record(_record("candidate-a", "sent"), when=datetime(2026, 8, 2, 10, 0))
    assert database.has_status("candidate-a", ("sent",)) is True
    assert database.today_sent_count(datetime(2026, 8, 2).date()) == 1
    with pytest.raises(Exception):
        database.record(_record("candidate-a", "sent"), when=datetime(2026, 8, 2, 11, 0))


def test_failed_does_not_count_as_sent(tmp_path) -> None:
    database = Database(tmp_path / "records.db")
    database.record(_record("candidate-a", "failed"), when=datetime(2026, 8, 2, 10, 0))
    database.record(_record("candidate-b", "dry_run_match"), when=datetime(2026, 8, 2, 10, 1))
    assert database.today_sent_count(datetime(2026, 8, 2).date()) == 0


def test_today_contacted_counts_unique_sent_and_unverified(tmp_path) -> None:
    database = Database(tmp_path / "records.db")
    when = datetime(2026, 8, 2, 10, 0)
    database.record(_record("candidate-a", "sent"), when=when)
    database.record(_record("candidate-b", "greeting_unverified"), when=when)
    database.record(_record("candidate-c", "failed"), when=when)
    assert database.today_contacted_count(when.date()) == 2
    stats = database.dashboard_stats(when.date())
    assert stats.contacted == 2
    assert stats.sent == 1
    assert stats.unverified == 1
    assert stats.failed == 1


def test_reservation_is_atomic(tmp_path) -> None:
    database = Database(tmp_path / "records.db")
    assert database.reserve("candidate-a") is True
    assert database.reserve("candidate-a") is False
    database.finalize(_record("candidate-a", "failed"))
    assert database.reserve("candidate-a") is True


def test_dry_run_record_does_not_block_later_formal_mode(tmp_path) -> None:
    database = Database(tmp_path / "records.db")
    database.record(_record("candidate-a", "dry_run_match"))
    assert database.already_handled("candidate-a", dry_run=True) is True
    assert database.already_handled("candidate-a", dry_run=False) is False


def test_unverified_greeting_is_never_retried(tmp_path) -> None:
    database = Database(tmp_path / "records.db")
    database.record(_record("candidate-a", "greeting_unverified"))
    assert database.already_handled("candidate-a", dry_run=True) is True
    assert database.already_handled("candidate-a", dry_run=False) is True


def test_reply_sent_count_only_counts_verified_replies(tmp_path) -> None:
    database = Database(tmp_path / "records.db")
    when = datetime(2026, 8, 2, 10, 0)
    database.record_reply(ReplyRecord("reply_sent", "已确认面试时间"), when=when)
    database.record_reply(ReplyRecord("reply_failed", "另一条话术", "未找到发送按钮"), when=when)
    database.record_reply(ReplyRecord("reply_unverified", "结果未知"), when=when)

    assert database.today_reply_sent_count(when.date()) == 1
    assert database.dashboard_stats(when.date()).replied == 1


def test_database_counts_verified_replies_per_conversation(tmp_path) -> None:
    database = Database(tmp_path / "reply-conversation.db")
    database.record_reply(ReplyRecord("reply_sent", "a", conversation_key="c1"))
    database.record_reply(ReplyRecord("reply_failed", "b", conversation_key="c1"))
    database.record_reply(ReplyRecord("reply_sent", "c", conversation_key="c2"))

    assert database.conversation_reply_sent_count("c1") == 1
    assert database.conversation_reply_sent_count("c2") == 1


def test_database_returns_last_verified_reply_time(tmp_path) -> None:
    database = Database(tmp_path / "reply-time.db")
    first = datetime(2026, 8, 4, 10, 0, tzinfo=timezone.utc)
    latest = first + timedelta(minutes=5)
    database.record_reply(ReplyRecord("reply_sent", "a"), when=first)
    database.record_reply(
        ReplyRecord("reply_failed", "ignored"),
        when=latest + timedelta(minutes=5),
    )
    database.record_reply(ReplyRecord("reply_sent", "b"), when=latest)

    assert database.last_reply_sent_at() == latest
