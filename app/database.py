from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterator


VALID_STATUSES = {
    "processing",
    "dry_run_match",
    "sent",
    "filtered",
    "duplicate",
    "failed",
    "greeting_unverified",
    "manual_skip",
}

VALID_REPLY_STATUSES = {
    "reply_dry_run",
    "reply_sent",
    "reply_failed",
    "reply_unverified",
    "reply_skipped",
}


@dataclass(frozen=True, slots=True)
class CandidateRecord:
    candidate_key: str
    candidate_name: str = ""
    candidate_url: str = ""
    candidate_summary: str = ""
    status: str = "failed"
    message: str = ""
    error_message: str = ""


@dataclass(frozen=True, slots=True)
class ReplyRecord:
    status: str
    message: str
    error_message: str = ""
    conversation_key: str = ""
    incoming_key: str = ""
    rule_name: str = ""


@dataclass(frozen=True, slots=True)
class DashboardStats:
    contacted: int = 0
    sent: int = 0
    unverified: int = 0
    failed: int = 0
    replied: int = 0


class Database:
    """每次操作使用独立连接，避免跨线程复用 sqlite3.Connection。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.RLock()
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._write_lock, self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS candidate_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    candidate_key TEXT NOT NULL,
                    candidate_name TEXT NOT NULL DEFAULT '',
                    candidate_url TEXT NOT NULL DEFAULT '',
                    candidate_summary TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    created_date TEXT NOT NULL,
                    error_message TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_candidate_records_key
                    ON candidate_records(candidate_key);
                CREATE INDEX IF NOT EXISTS idx_candidate_records_date_status
                    ON candidate_records(created_date, status);
                CREATE UNIQUE INDEX IF NOT EXISTS idx_one_sent_per_candidate
                    ON candidate_records(candidate_key) WHERE status = 'sent';

                CREATE TABLE IF NOT EXISTS candidate_reservations (
                    candidate_key TEXT PRIMARY KEY,
                    reserved_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS reply_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    status TEXT NOT NULL,
                    message TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    created_date TEXT NOT NULL,
                    error_message TEXT NOT NULL DEFAULT '',
                    conversation_key TEXT NOT NULL DEFAULT '',
                    incoming_key TEXT NOT NULL DEFAULT '',
                    rule_name TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS idx_reply_records_date_status
                    ON reply_records(created_date, status);
                """
            )
            reply_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(reply_records)").fetchall()
            }
            for column in ("conversation_key", "incoming_key", "rule_name"):
                if column not in reply_columns:
                    connection.execute(
                        f"ALTER TABLE reply_records ADD COLUMN {column} TEXT NOT NULL DEFAULT ''"
                    )
            connection.execute(
                """
                CREATE UNIQUE INDEX IF NOT EXISTS idx_one_reply_per_incoming
                    ON reply_records(incoming_key)
                    WHERE incoming_key <> '' AND status IN ('reply_sent', 'reply_unverified')
                """
            )
            # 上次异常退出产生的临时锁不代表已发送，启动时安全释放。
            connection.execute("DELETE FROM candidate_reservations")

    def close(self) -> None:
        """连接均为短连接；保留此方法作为明确的生命周期边界。"""

    def record(self, record: CandidateRecord, *, when: datetime | None = None) -> int:
        if record.status not in VALID_STATUSES - {"processing"}:
            raise ValueError(f"未知状态：{record.status}")
        moment = when or datetime.now().astimezone()
        with self._write_lock, self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO candidate_records (
                    candidate_key, candidate_name, candidate_url, candidate_summary,
                    status, message, created_at, created_date, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.candidate_key,
                    record.candidate_name,
                    record.candidate_url,
                    record.candidate_summary,
                    record.status,
                    record.message,
                    moment.isoformat(timespec="seconds"),
                    moment.date().isoformat(),
                    record.error_message,
                ),
            )
            return int(cursor.lastrowid)

    def reserve(self, candidate_key: str) -> bool:
        try:
            with self._write_lock, self._connect() as connection:
                connection.execute(
                    "INSERT INTO candidate_reservations(candidate_key, reserved_at) VALUES (?, ?)",
                    (candidate_key, datetime.now().astimezone().isoformat(timespec="seconds")),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def release(self, candidate_key: str) -> None:
        with self._write_lock, self._connect() as connection:
            connection.execute("DELETE FROM candidate_reservations WHERE candidate_key = ?", (candidate_key,))

    def finalize(self, record: CandidateRecord) -> int:
        """记录最终状态并在同一事务中释放候选人预留。"""
        if record.status == "processing" or record.status not in VALID_STATUSES:
            raise ValueError(f"无效最终状态：{record.status}")
        moment = datetime.now().astimezone()
        with self._write_lock, self._connect() as connection:
            try:
                cursor = connection.execute(
                    """
                    INSERT INTO candidate_records (
                        candidate_key, candidate_name, candidate_url, candidate_summary,
                        status, message, created_at, created_date, error_message
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.candidate_key,
                        record.candidate_name,
                        record.candidate_url,
                        record.candidate_summary,
                        record.status,
                        record.message,
                        moment.isoformat(timespec="seconds"),
                        moment.date().isoformat(),
                        record.error_message,
                    ),
                )
                connection.execute(
                    "DELETE FROM candidate_reservations WHERE candidate_key = ?",
                    (record.candidate_key,),
                )
                return int(cursor.lastrowid)
            except Exception:
                connection.rollback()
                raise

    def has_status(self, candidate_key: str, statuses: tuple[str, ...]) -> bool:
        if not statuses:
            return False
        placeholders = ",".join("?" for _ in statuses)
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT 1 FROM candidate_records WHERE candidate_key = ? AND status IN ({placeholders}) LIMIT 1",
                (candidate_key, *statuses),
            ).fetchone()
        return row is not None

    def already_handled(self, candidate_key: str, *, dry_run: bool) -> bool:
        # A greeting whose click outcome could not be verified must never be
        # retried automatically: the platform may already have sent it.
        statuses = ("sent", "filtered", "greeting_unverified", "manual_skip")
        if dry_run:
            statuses = (*statuses, "dry_run_match")
        return self.has_status(candidate_key, statuses)

    def today_sent_count(self, day: date | None = None) -> int:
        target = (day or date.today()).isoformat()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM candidate_records WHERE created_date = ? AND status = 'sent'",
                (target,),
            ).fetchone()
        return int(row["count"])

    def today_contacted_count(self, day: date | None = None) -> int:
        return self.dashboard_stats(day).contacted

    def dashboard_stats(self, day: date | None = None) -> DashboardStats:
        """Return one transactionally consistent daily dashboard snapshot."""
        target = (day or date.today()).isoformat()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT
                    COUNT(DISTINCT CASE
                        WHEN status IN ('sent', 'greeting_unverified') THEN candidate_key
                    END) AS contacted,
                    COUNT(DISTINCT CASE WHEN status = 'sent' THEN candidate_key END) AS sent,
                    COUNT(DISTINCT CASE
                        WHEN status = 'greeting_unverified' THEN candidate_key
                    END) AS unverified,
                    COUNT(DISTINCT CASE WHEN status = 'failed' THEN candidate_key END) AS failed
                FROM candidate_records
                WHERE created_date = ?
                """,
                (target,),
            ).fetchone()
            reply_row = connection.execute(
                "SELECT COUNT(*) AS count FROM reply_records WHERE created_date = ? AND status = 'reply_sent'",
                (target,),
            ).fetchone()
        return DashboardStats(
            contacted=int(row["contacted"]),
            sent=int(row["sent"]),
            unverified=int(row["unverified"]),
            failed=int(row["failed"]),
            replied=int(reply_row["count"]),
        )

    def status_counts(self, day: date | None = None) -> dict[str, int]:
        target = (day or date.today()).isoformat()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM candidate_records WHERE created_date = ? GROUP BY status",
                (target,),
            ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}

    def record_reply(self, record: ReplyRecord, *, when: datetime | None = None) -> int:
        if record.status not in VALID_REPLY_STATUSES:
            raise ValueError(f"未知回复状态：{record.status}")
        moment = when or datetime.now().astimezone()
        with self._write_lock, self._connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO reply_records (
                    status, message, created_at, created_date, error_message,
                    conversation_key, incoming_key, rule_name
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.status,
                    record.message,
                    moment.isoformat(timespec="seconds"),
                    moment.date().isoformat(),
                    record.error_message,
                    record.conversation_key,
                    record.incoming_key,
                    record.rule_name,
                ),
            )
            return int(cursor.lastrowid)

    def has_replied_to(self, incoming_key: str) -> bool:
        if not incoming_key:
            return False
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM reply_records
                WHERE incoming_key = ? AND status IN ('reply_sent', 'reply_unverified')
                LIMIT 1
                """,
                (incoming_key,),
            ).fetchone()
        return row is not None

    def today_reply_sent_count(self, day: date | None = None) -> int:
        target = (day or date.today()).isoformat()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS count FROM reply_records WHERE created_date = ? AND status = 'reply_sent'",
                (target,),
            ).fetchone()
        return int(row["count"])

    def conversation_reply_sent_count(
        self,
        conversation_key: str,
        day: date | None = None,
    ) -> int:
        if not conversation_key:
            return 0
        target = (day or date.today()).isoformat()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM reply_records
                WHERE created_date = ?
                  AND conversation_key = ?
                  AND status = 'reply_sent'
                """,
                (target, conversation_key),
            ).fetchone()
        return int(row["count"])

    def last_reply_sent_at(self) -> datetime | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT created_at
                FROM reply_records
                WHERE status = 'reply_sent'
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """
            ).fetchone()
        if row is None:
            return None
        return datetime.fromisoformat(str(row["created_at"]))
