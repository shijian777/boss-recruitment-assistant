from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path


VALID_STATUSES = {
    "processing",
    "dry_run_match",
    "sent",
    "filtered",
    "duplicate",
    "failed",
    "manual_skip",
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


class Database:
    """每次操作使用独立连接，避免跨线程复用 sqlite3.Connection。"""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.RLock()
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        return connection

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
        statuses = ("sent", "filtered", "manual_skip")
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

    def status_counts(self, day: date | None = None) -> dict[str, int]:
        target = (day or date.today()).isoformat()
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM candidate_records WHERE created_date = ? GROUP BY status",
                (target,),
            ).fetchall()
        return {str(row["status"]): int(row["count"]) for row in rows}
