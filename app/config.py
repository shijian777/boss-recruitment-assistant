from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.candidate_filter import CandidateFilterSettings
from app.reply_rules import ReplyRule


DEFAULT_CONFIG: dict[str, Any] = {
    "control_mode": "desktop",
    "dry_run": True,
    "auto_start": False,
    "auto_scroll": True,
    "message": "您好，我们正在招聘相关岗位，您的经历与岗位需求较为匹配，方便进一步沟通吗？",
    "auto_reply": {
        "enabled": False,
        "poll_seconds": 10,
        "daily_limit": 20,
        "per_conversation_limit": 3,
        "interval_seconds": 10,
        "stop_keywords": ["不感兴趣", "不考虑", "不用了", "不要联系", "已找到工作"],
        "rules": [],
    },
    "candidate_filter": {
        "keywords": [],
        "cities": [],
        "salary_min_k": None,
        "salary_max_k": None,
    },
    "session_limit": 20,
    "daily_limit": 20,
    "interval_seconds": 5,
    "batch_size": 20,
    "batch_pause_seconds": 0,
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
        "continuation_button_texts": ["继续沟通"],
        "greeting_success_texts": ["已向牛人发送招呼"],
        "greeting_blocked_texts": [
            "招聘权益不足",
            "沟通权益不足",
            "招呼次数已用完",
            "招聘力不足",
            "去充值",
            "立即充值",
            "购买权益",
        ],
        "greeting_dismiss_button_texts": ["知道了"],
        "benign_dismiss_button_texts": ["我知道了"],
        "message_input_control_types": ["Edit", "Document"],
        "message_input_name_keywords": ["输入消息", "请输入消息", "发消息", "说点什么"],
        "composer_activation_actions": ["激活"],
        "send_button_texts": ["发送"],
        "message_item_control_types": ["Text", "ListItem", "Document"],
        "chat_message_control_types": ["Text", "ListItem", "Group"],
        "conversation_header_control_types": ["Text", "Hyperlink"],
        "incoming_message_markers": ["对方发送的消息", "收到的消息", "候选人消息", "牛人消息"],
        "outgoing_message_markers": ["我发送的消息", "发出的消息", "己方消息", "已读", "送达"],
        "message_nav_texts": ["消息"],
        "inbox_sent_status_texts": ["[已读]", "[送达]"],
        "next_page_texts": ["下一页"],
    },
}


class ConfigError(ValueError):
    """用户可修复的配置错误。"""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("配置错误：\n- " + "\n- ".join(errors))


@dataclass(frozen=True, slots=True)
class AutoReplyConfig:
    enabled: bool
    poll_seconds: int
    daily_limit: int
    per_conversation_limit: int
    interval_seconds: int
    stop_keywords: tuple[str, ...]
    rules: tuple[ReplyRule, ...]


@dataclass(frozen=True, slots=True)
class AppConfig:
    control_mode: str
    dry_run: bool
    auto_start: bool
    auto_scroll: bool
    message: str
    auto_reply: AutoReplyConfig
    candidate_filter: CandidateFilterSettings
    session_limit: int
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
            auto_start=raw["auto_start"],
            auto_scroll=raw["auto_scroll"],
            message=raw["message"].strip(),
            auto_reply=AutoReplyConfig(
                enabled=raw["auto_reply"]["enabled"],
                poll_seconds=raw["auto_reply"]["poll_seconds"],
                daily_limit=raw["auto_reply"]["daily_limit"],
                per_conversation_limit=raw["auto_reply"]["per_conversation_limit"],
                interval_seconds=raw["auto_reply"]["interval_seconds"],
                stop_keywords=tuple(raw["auto_reply"]["stop_keywords"]),
                rules=tuple(
                    ReplyRule(
                        name=item["name"].strip(),
                        keywords=tuple(keyword.strip() for keyword in item["keywords"]),
                        reply=item["reply"].strip(),
                        match_mode=item.get("match_mode", "any"),
                        enabled=item.get("enabled", True),
                    )
                    for item in raw["auto_reply"]["rules"]
                ),
            ),
            candidate_filter=CandidateFilterSettings(
                keywords=tuple(raw["candidate_filter"]["keywords"]),
                cities=tuple(raw["candidate_filter"]["cities"]),
                salary_min_k=raw["candidate_filter"]["salary_min_k"],
                salary_max_k=raw["candidate_filter"]["salary_max_k"],
            ),
            session_limit=raw["session_limit"],
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


def _validate_string_list(
    raw: dict[str, Any],
    key: str,
    errors: list[str],
    *,
    allow_empty: bool = True,
    label: str | None = None,
) -> None:
    field_name = label or key
    value = raw.get(key)
    if not isinstance(value, list):
        errors.append(f"{field_name} 必须是字符串数组")
        return
    if not allow_empty and not value:
        errors.append(f"{field_name} 不能为空")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        errors.append(f"{field_name} 中只能包含非空字符串")


def validate_config(raw: object) -> list[str]:
    if not isinstance(raw, dict):
        return ["配置根节点必须是 JSON 对象"]

    errors: list[str] = []
    required = {
        "control_mode",
        "dry_run",
        "auto_start",
        "auto_scroll",
        "message",
        "auto_reply",
        "candidate_filter",
        "session_limit",
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
    if raw.get("auto_start") is not True and raw.get("auto_start") is not False:
        errors.append("auto_start 必须是 true 或 false")
    if raw.get("auto_scroll") is not True and raw.get("auto_scroll") is not False:
        errors.append("auto_scroll 必须是 true 或 false")
    if not isinstance(raw.get("message"), str) or not raw.get("message", "").strip():
        errors.append("message 邀约话术不能为空")

    auto_reply = raw.get("auto_reply")
    if not isinstance(auto_reply, dict):
        errors.append("auto_reply 必须是 JSON 对象")
    else:
        if auto_reply.get("enabled") is not True and auto_reply.get("enabled") is not False:
            errors.append("auto_reply.enabled 必须是 true 或 false")
        poll_seconds = auto_reply.get("poll_seconds")
        if not _is_int(poll_seconds) or not 2 <= poll_seconds <= 3600:
            errors.append("auto_reply.poll_seconds 必须是 2 到 3600 之间的整数")
        reply_daily_limit = auto_reply.get("daily_limit")
        if not _is_int(reply_daily_limit) or not 1 <= reply_daily_limit <= 100:
            errors.append("auto_reply.daily_limit 必须是 1 到 100 之间的整数")
        per_conversation_limit = auto_reply.get("per_conversation_limit")
        if not _is_int(per_conversation_limit) or not 1 <= per_conversation_limit <= 20:
            errors.append("auto_reply.per_conversation_limit 必须是 1 到 20 之间的整数")
        reply_interval_seconds = auto_reply.get("interval_seconds")
        if not _is_int(reply_interval_seconds) or not 1 <= reply_interval_seconds <= 3600:
            errors.append("auto_reply.interval_seconds 必须是 1 到 3600 之间的整数")
        _validate_string_list(auto_reply, "stop_keywords", errors, allow_empty=False)
        rules = auto_reply.get("rules")
        if not isinstance(rules, list):
            errors.append("auto_reply.rules 必须是数组")
        elif len(rules) > 50:
            errors.append("auto_reply.rules 最多允许 50 条话术")
        else:
            names: set[str] = set()
            enabled_triggers: dict[str, str] = {}
            for index, item in enumerate(rules):
                prefix = f"auto_reply.rules[{index}]"
                if not isinstance(item, dict):
                    errors.append(f"{prefix} 必须是 JSON 对象")
                    continue
                name = item.get("name")
                if not isinstance(name, str) or not name.strip():
                    errors.append(f"{prefix}.name 必须是非空字符串")
                elif name.strip() in names:
                    errors.append(f"{prefix}.name 不能重复")
                else:
                    names.add(name.strip())
                keywords = item.get("keywords")
                if not isinstance(keywords, list) or not keywords:
                    errors.append(f"{prefix}.keywords 必须是非空字符串数组")
                elif any(not isinstance(value, str) or not value.strip() for value in keywords):
                    errors.append(f"{prefix}.keywords 中只能包含非空字符串")
                reply = item.get("reply")
                if not isinstance(reply, str) or not reply.strip():
                    errors.append(f"{prefix}.reply 必须是非空字符串")
                elif len(reply) > 1000:
                    errors.append(f"{prefix}.reply 不能超过 1000 个字符")
                if item.get("match_mode", "any") not in {"any", "all"}:
                    errors.append(f"{prefix}.match_mode 只能是 any 或 all")
                enabled = item.get("enabled", True)
                if enabled is not True and enabled is not False:
                    errors.append(f"{prefix}.enabled 必须是 true 或 false")
                if enabled is True and isinstance(keywords, list):
                    for keyword in keywords:
                        if not isinstance(keyword, str) or not keyword.strip():
                            continue
                        normalized = keyword.strip().casefold()
                        previous = enabled_triggers.get(normalized)
                        if previous is not None:
                            errors.append(
                                f"{prefix}.keywords 触发词“{keyword.strip()}”与“{previous}”重复"
                            )
                        else:
                            enabled_triggers[normalized] = (
                                name.strip() if isinstance(name, str) else prefix
                            )

    candidate_filter = raw.get("candidate_filter")
    if not isinstance(candidate_filter, dict):
        errors.append("candidate_filter 必须是 JSON 对象")
    else:
        _validate_string_list(
            candidate_filter,
            "keywords",
            errors,
            label="candidate_filter.keywords",
        )
        _validate_string_list(
            candidate_filter,
            "cities",
            errors,
            label="candidate_filter.cities",
        )
        salary_minimum = candidate_filter.get("salary_min_k")
        salary_maximum = candidate_filter.get("salary_max_k")
        for field_name, value in (
            ("salary_min_k", salary_minimum),
            ("salary_max_k", salary_maximum),
        ):
            if value is not None and (not _is_int(value) or not 1 <= value <= 1000):
                errors.append(
                    f"candidate_filter.{field_name} 必须为空或 1 到 1000 之间的整数"
                )
        if (
            _is_int(salary_minimum)
            and _is_int(salary_maximum)
            and salary_minimum > salary_maximum
        ):
            errors.append("candidate_filter 最低薪资不能高于最高薪资")

    positive_fields = ("session_limit", "daily_limit", "batch_size", "max_consecutive_failures")
    for key in positive_fields:
        value = raw.get(key)
        if not _is_int(value) or value <= 0:
            errors.append(f"{key} 必须是正整数")
    if _is_int(raw.get("daily_limit")) and raw["daily_limit"] > 200:
        errors.append("daily_limit 不应超过 200；请设置合理的人工邀约上限")
    if _is_int(raw.get("session_limit")) and raw["session_limit"] > 200:
        errors.append("session_limit 不应超过 200；请设置合理的单次邀约上限")

    interval_seconds = raw.get("interval_seconds")
    if not _is_int(interval_seconds) or not 1 <= interval_seconds <= 3600:
        errors.append("interval_seconds 必须是 1 到 3600 之间的整数")
    batch_pause_seconds = raw.get("batch_pause_seconds")
    if not _is_int(batch_pause_seconds) or batch_pause_seconds < 0:
        errors.append("batch_pause_seconds 必须是大于或等于 0 的整数")

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
        "continuation_button_texts",
        "greeting_success_texts",
        "greeting_blocked_texts",
        "greeting_dismiss_button_texts",
        "benign_dismiss_button_texts",
        "message_input_control_types",
        "message_input_name_keywords",
        "composer_activation_actions",
        "send_button_texts",
        "message_item_control_types",
        "chat_message_control_types",
        "conversation_header_control_types",
        "incoming_message_markers",
        "outgoing_message_markers",
        "message_nav_texts",
        "inbox_sent_status_texts",
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


def _read_config_object(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigError([f"未找到 {path.name}，请复制 config.example.json 后再启动"])
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError([f"JSON 格式错误（第 {exc.lineno} 行，第 {exc.colno} 列）：{exc.msg}"]) from exc
    except OSError as exc:
        raise ConfigError([f"无法读取配置：{exc}"]) from exc
    if not isinstance(raw, dict):
        raise ConfigError(["配置根节点必须是 JSON 对象"])
    # Older installations did not have session_limit. Use the old batch size
    # once during migration so an update does not invalidate user settings.
    if "session_limit" not in raw:
        raw["session_limit"] = raw.get("batch_size", DEFAULT_CONFIG["session_limit"])
    if "auto_reply" not in raw:
        raw["auto_reply"] = deepcopy(DEFAULT_CONFIG["auto_reply"])
    elif isinstance(raw["auto_reply"], dict):
        for key in ("per_conversation_limit", "interval_seconds"):
            raw["auto_reply"].setdefault(
                key,
                deepcopy(DEFAULT_CONFIG["auto_reply"][key]),
            )
        rules = raw["auto_reply"].get("rules")
        if isinstance(rules, list):
            for item in rules:
                if isinstance(item, dict):
                    item.setdefault("enabled", True)
    if "candidate_filter" not in raw:
        legacy_keywords = raw.get("include_keywords", [])
        raw["candidate_filter"] = deepcopy(DEFAULT_CONFIG["candidate_filter"])
        if isinstance(legacy_keywords, list):
            raw["candidate_filter"]["keywords"] = deepcopy(legacy_keywords)
    selectors = raw.get("selectors")
    if isinstance(selectors, dict):
        for key in (
            "greeting_blocked_texts",
            "chat_message_control_types",
            "conversation_header_control_types",
            "incoming_message_markers",
            "outgoing_message_markers",
            "message_nav_texts",
            "inbox_sent_status_texts",
            "composer_activation_actions",
        ):
            if key not in selectors:
                selectors[key] = deepcopy(DEFAULT_CONFIG["selectors"][key])
    return raw


def load_config(path: Path) -> AppConfig:
    return AppConfig.from_dict(_read_config_object(path))


def save_runtime_settings(
    path: Path,
    *,
    session_limit: int,
    interval_seconds: int,
    daily_limit: int,
    message: str,
    dry_run: bool | None = None,
    auto_reply_enabled: bool | None = None,
    reply_rules: tuple[ReplyRule, ...] | None = None,
    candidate_filter: CandidateFilterSettings | None = None,
    auto_reply_poll_seconds: int | None = None,
    auto_reply_daily_limit: int | None = None,
    auto_reply_per_conversation_limit: int | None = None,
    auto_reply_interval_seconds: int | None = None,
    auto_reply_stop_keywords: tuple[str, ...] | None = None,
) -> AppConfig:
    """Validate and atomically persist settings editable from the main panel."""
    raw = _read_config_object(path)
    raw.update(
        session_limit=session_limit,
        interval_seconds=interval_seconds,
        daily_limit=daily_limit,
        message=message,
    )
    if dry_run is not None:
        raw["dry_run"] = dry_run
    if candidate_filter is not None:
        raw["candidate_filter"] = {
            "keywords": list(candidate_filter.keywords),
            "cities": list(candidate_filter.cities),
            "salary_min_k": candidate_filter.salary_min_k,
            "salary_max_k": candidate_filter.salary_max_k,
        }
    auto_reply = deepcopy(raw["auto_reply"])
    if auto_reply_enabled is not None:
        auto_reply["enabled"] = auto_reply_enabled
    if reply_rules is not None:
        auto_reply["rules"] = [
            {
                "name": rule.name,
                "keywords": list(rule.keywords),
                "reply": rule.reply,
                "match_mode": rule.match_mode,
                "enabled": rule.enabled,
            }
            for rule in reply_rules
        ]
    if auto_reply_poll_seconds is not None:
        auto_reply["poll_seconds"] = auto_reply_poll_seconds
    if auto_reply_daily_limit is not None:
        auto_reply["daily_limit"] = auto_reply_daily_limit
    if auto_reply_per_conversation_limit is not None:
        auto_reply["per_conversation_limit"] = auto_reply_per_conversation_limit
    if auto_reply_interval_seconds is not None:
        auto_reply["interval_seconds"] = auto_reply_interval_seconds
    if auto_reply_stop_keywords is not None:
        auto_reply["stop_keywords"] = list(auto_reply_stop_keywords)
    raw["auto_reply"] = auto_reply
    config = AppConfig.from_dict(raw)
    temporary = path.with_name(f"{path.name}.tmp")
    try:
        temporary.write_text(
            json.dumps(raw, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise ConfigError([f"无法保存配置：{exc}"]) from exc
    return config
