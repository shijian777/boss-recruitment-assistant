from __future__ import annotations

import threading
import time

from app.safety import RunControl, detect_risk, evaluate_keywords


def test_keyword_any_mode() -> None:
    result = evaluate_keywords("资深  PYTHON   后端工程师", ["python", "java"], [], "any")
    assert result.matched is True


def test_keyword_all_mode() -> None:
    assert evaluate_keywords("Python 后端", ["python", "后端"], [], "all").matched is True
    assert evaluate_keywords("Python 后端", ["python", "前端"], [], "all").matched is False


def test_exclude_keyword_takes_priority() -> None:
    result = evaluate_keywords("Python 后端 兼职", ["python"], ["兼职"], "any")
    assert result.matched is False
    assert "排除关键词" in result.reason


def test_risk_keyword_detection() -> None:
    assert detect_risk("系统提示：请完成验证后继续", ["验证码", "请完成验证"]) == "请完成验证"
    assert detect_risk("候选人列表正常", ["验证码"]) is None


def test_stop_interrupts_paused_wait() -> None:
    control = RunControl(initially_paused=True)
    result: list[bool] = []
    thread = threading.Thread(target=lambda: result.append(control.interruptible_sleep(10)))
    thread.start()
    time.sleep(0.05)
    control.stop()
    thread.join(timeout=1)
    assert not thread.is_alive()
    assert result == [False]


def test_resume_clears_pause() -> None:
    control = RunControl(initially_paused=True)
    control.resume()
    assert control.is_paused is False
    assert control.interruptible_sleep(0) is True
