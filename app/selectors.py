"""选择器相关常量和小工具。

真实站点可能改版，因此具体 CSS 选择器保留在 config.json 中；这里仅放与
Playwright DOM 查询无关的文本规则，便于离线测试。
"""

from __future__ import annotations

import re


def normalize_space(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def exact_action_label(value: str | None, allowed: tuple[str, ...] | list[str]) -> bool:
    text = normalize_space(value)
    return any(text == normalize_space(item) for item in allowed)
