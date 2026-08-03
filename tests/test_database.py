from __future__ import annotations

from datetime import datetime

import pytest

from app.database import CandidateRecord, Database


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
