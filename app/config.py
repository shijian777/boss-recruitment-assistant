from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_CONFIG: dict[str, Any] = {
    "control_mode": "desktop",
    "dry_run": True,
    "message": "您好，我们正在招聘相关岗位，您的经历与岗位需求较为匹配，方便进一步沟通吗？",
    "daily_limit": 20,
    "interval_seconds": 60,
    "batch_size": 5,
    "batch_pause_seconds": 300,
    "max_consecutive_failures": 3,
    "include_keywords": [],
    "exclude_keywords": [],
    "match_mode": "any",
    "risk_keywords": [
        "验证码",
        "安全验证",
        "请完成验证",
        "操作频繁",
        "访问异常",
        "账号异常",
        "登录异常",
        "行为异常",
        "风险提示",
        "系统检测",
    ],
    "diagnostics_save_tree": False,
    "desktop_window_title": "BOSS直聘",
    "desktop_process_name": "boss-zhipin.exe",
    "selectors": {
        "candidate_container_control_types": ["ListItem", "Group", "Pane"],
        "candidate_name_control_types": ["Text", "Hyperlink"],
        "greeting_button_texts": ["打招呼", "立即沟通", "聊一聊"],
        "message_input_control_types": ["Edit", "Document"],
        "message_input_name_keywords": ["输入消息", "请输入消息", "发消息", "说点什么"],
        "send_button_texts": ["发送"],
        "message_item_control_types": ["Text", "ListItem", "Document"],
        "next_page_texts": ["下一页"],
    },
}


class ConfigError(ValueError):
    """用户可修复的配置错误。"""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("配置错误：\n- " + "\n- ".join(errors))


@dataclass(frozen=True, slots=True)
class AppConfig:
    control_mode: str
    dry_run: bool
    message: str
    daily_limit: int
    interval_seconds: int
    batch_size: int
    batch_pause_seconds: int
    max_consecutive_failures: int
    include_keywords: tuple[str, ...]
    exclude_keywords: tuple[str, ...]
    match_mode: str
    risk_keywords: tuple[str, ...]
    diagnostics_save_tree: bool
    desktop_window_title: str
    desktop_process_name: str
    selectors: dict[str, tuple[str, ...]]

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "AppConfig":
        errors = validate_config(raw)
        if errors:
            raise ConfigError(errors)
        selectors = {
            key: tuple(value.strip() for value in values if value.strip())
            for key, values in raw["selectors"].items()
        }
        return cls(
            control_mode=raw["control_mode"],
            dry_run=raw["dry_run"],
            message=raw["message"].strip(),
            daily_limit=raw["daily_limit"],
            interval_seconds=raw["interval_seconds"],
            batch_size=raw["batch_size"],
            batch_pause_seconds=raw["batch_pause_seconds"],
            max_consecutive_failures=raw["max_consecutive_failures"],
            include_keywords=tuple(raw["include_keywords"]),
            exclude_keywords=tuple(raw["exclude_keywords"]),
            match_mode=raw["match_mode"],
            risk_keywords=tuple(raw["risk_keywords"]),
            diagnostics_save_tree=raw["diagnostics_save_tree"],
            desktop_window_title=raw["desktop_window_title"].strip(),
            desktop_process_name=raw["desktop_process_name"].strip(),
            selectors=selectors,
        )

    @classmethod
    def defaults(cls) -> "AppConfig":
        return cls.from_dict(deepcopy(DEFAULT_CONFIG))


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _validate_string_list(raw: dict[str, Any], key: str, errors: list[str], *, allow_empty: bool = True) -> None:
    value = raw.get(key)
    if not isinstance(value, list):
        errors.append(f"{key} 必须是字符串数组")
        return
    if not allow_empty and not value:
        errors.append(f"{key} 不能为空")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        errors.append(f"{key} 中只能包含非空字符串")


def validate_config(raw: object) -> list[str]:
    if not isinstance(raw, dict):
        return ["配置根节点必须是 JSON 对象"]

    errors: list[str] = []
    required = {
        "control_mode",
        "dry_run",
        "message",
        "daily_limit",
        "interval_seconds",
        "batch_size",
        "batch_pause_seconds",
        "max_consecutive_failures",
        "include_keywords",
        "exclude_keywords",
        "match_mode",
        "risk_keywords",
        "diagnostics_save_tree",
        "desktop_window_title",
        "desktop_process_name",
        "selectors",
    }
    for key in sorted(required - raw.keys()):
        errors.append(f"缺少必填字段：{key}")

    if raw.get("control_mode") != "desktop":
        errors.append("control_mode 当前只能是 desktop")
    if raw.get("dry_run") is not True and raw.get("dry_run") is not False:
        errors.append("dry_run 必须是 true 或 false")
    if not isinstance(raw.get("message"), str) or not raw.get("message", "").strip():
        errors.append("message 邀约话术不能为空")

    positive_fields = ("daily_limit", "batch_size", "max_consecutive_failures")
    for key in positive_fields:
        value = raw.get(key)
        if not _is_int(value) or value <= 0:
            errors.append(f"{key} 必须是正整数")
    if _is_int(raw.get("daily_limit")) and raw["daily_limit"] > 200:
        errors.append("daily_limit 不应超过 200；请设置合理的人工邀约上限")

    for key in ("interval_seconds", "batch_pause_seconds"):
        value = raw.get(key)
        if not _is_int(value) or value < 0:
            errors.append(f"{key} 必须是大于或等于 0 的整数")

    _validate_string_list(raw, "include_keywords", errors)
    _validate_string_list(raw, "exclude_keywords", errors)
    _validate_string_list(raw, "risk_keywords", errors, allow_empty=False)

    if raw.get("match_mode") not in {"any", "all"}:
        errors.append("match_mode 只能是 any 或 all")
    if raw.get("diagnostics_save_tree") is not True and raw.get("diagnostics_save_tree") is not False:
        errors.append("diagnostics_save_tree 必须是 true 或 false")
    if not isinstance(raw.get("desktop_window_title"), str) or not raw.get("desktop_window_title", "").strip():
        errors.append("desktop_window_title 必须是非空字符串")
    if not isinstance(raw.get("desktop_process_name"), str) or not raw.get("desktop_process_name", "").strip():
        errors.append("desktop_process_name 必须是非空字符串")

    selectors = raw.get("selectors")
    required_selectors = {
        "candidate_container_control_types",
        "candidate_name_control_types",
        "greeting_button_texts",
        "message_input_control_types",
        "message_input_name_keywords",
        "send_button_texts",
        "message_item_control_types",
        "next_page_texts",
    }
    if not isinstance(selectors, dict):
        errors.append("selectors 必须是 JSON 对象")
    else:
        for key in sorted(required_selectors):
            if key not in selectors:
                errors.append(f"selectors 缺少字段：{key}")
            elif not isinstance(selectors[key], list) or not selectors[key]:
                errors.append(f"selectors.{key} 必须是非空数组")
            elif any(not isinstance(item, str) or not item.strip() for item in selectors[key]):
                errors.append(f"selectors.{key} 中只能包含非空字符串")
    return errors


def load_config(path: Path) -> AppConfig:
    if not path.exists():
        raise ConfigError([f"未找到 {path.name}，请复制 config.example.json 后再启动"])
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError([f"JSON 格式错误（第 {exc.lineno} 行，第 {exc.colno} 列）：{exc.msg}"]) from exc
    except OSError as exc:
        raise ConfigError([f"无法读取配置：{exc}"]) from exc
    return AppConfig.from_dict(raw)
