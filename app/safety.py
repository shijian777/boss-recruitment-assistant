from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from app.selectors import normalize_space


def normalize_for_match(value: str) -> str:
    return normalize_space(value).casefold()


@dataclass(frozen=True, slots=True)
class FilterDecision:
    matched: bool
    reason: str


def evaluate_keywords(
    text: str,
    include_keywords: tuple[str, ...] | list[str],
    exclude_keywords: tuple[str, ...] | list[str],
    match_mode: str,
) -> FilterDecision:
    haystack = normalize_for_match(text)
    excludes = [normalize_for_match(item) for item in exclude_keywords if normalize_for_match(item)]
    for keyword in excludes:
        if keyword in haystack:
            return FilterDecision(False, f"命中排除关键词：{keyword}")

    includes = [normalize_for_match(item) for item in include_keywords if normalize_for_match(item)]
    if not includes:
        return FilterDecision(True, "未设置包含关键词")
    hits = [keyword for keyword in includes if keyword in haystack]
    if match_mode == "all" and len(hits) != len(includes):
        missing = [item for item in includes if item not in hits]
        return FilterDecision(False, f"未命中全部包含关键词：{', '.join(missing)}")
    if match_mode == "any" and not hits:
        return FilterDecision(False, "未命中任一包含关键词")
    return FilterDecision(True, f"命中包含关键词：{', '.join(hits)}")


def detect_risk(text: str, risk_keywords: tuple[str, ...] | list[str]) -> str | None:
    haystack = normalize_for_match(text)
    for keyword in risk_keywords:
        normalized = normalize_for_match(keyword)
        if normalized and normalized in haystack:
            return keyword
    return None


class RunControl:
    """线程安全的暂停/停止控制器；所有等待都可被紧急停止打断。"""

    def __init__(self, *, initially_paused: bool = True) -> None:
        self._paused = threading.Event()
        self._stopped = threading.Event()
        if initially_paused:
            self._paused.set()

    @property
    def is_paused(self) -> bool:
        return self._paused.is_set()

    @property
    def is_stopped(self) -> bool:
        return self._stopped.is_set()

    def pause(self) -> None:
        self._paused.set()

    def resume(self) -> None:
        if not self._stopped.is_set():
            self._paused.clear()

    def stop(self) -> None:
        self._stopped.set()
        self._paused.clear()

    def wait_while_paused(self, poll_seconds: float = 0.1) -> bool:
        while self._paused.is_set() and not self._stopped.wait(poll_seconds):
            pass
        return not self._stopped.is_set()
    def interruptible_sleep(self, seconds: float, poll_seconds: float = 0.1) -> bool:
        """只累计非暂停时间；返回 False 表示收到停止。"""
        remaining = max(0.0, float(seconds))
        previous = time.monotonic()
        while remaining > 0:
            if self._stopped.wait(min(poll_seconds, remaining)):
                return False
            now = time.monotonic()
            if not self._paused.is_set():
                remaining -= now - previous
            previous = now
            if not self.wait_while_paused(poll_seconds):
                return False
        return not self._stopped.is_set()
