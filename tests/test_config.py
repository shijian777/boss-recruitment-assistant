from __future__ import annotations

import json
from copy import deepcopy

import pytest

from app.candidate_filter import CandidateFilterSettings
from app.config import (
    DEFAULT_CONFIG,
    AppConfig,
    ConfigError,
    load_config,
    save_runtime_settings,
    validate_config,
)
from app.reply_rules import ReplyRule


def test_default_config_is_valid_and_safe() -> None:
    config = AppConfig.defaults()
    assert config.control_mode == "desktop"
    assert config.dry_run is True
    assert config.auto_start is False
    assert config.auto_scroll is True
    assert config.auto_reply.enabled is False
    assert config.auto_reply.rules == ()
    assert config.auto_reply.per_conversation_limit == 3
    assert config.auto_reply.interval_seconds == 10
    assert config.candidate_filter == CandidateFilterSettings()
    assert config.session_limit == 20
    assert config.selectors["benign_dismiss_button_texts"] == ("我知道了",)
    assert "去充值" in config.selectors["greeting_blocked_texts"]
    assert config.interval_seconds == 5
    assert validate_config(deepcopy(DEFAULT_CONFIG)) == []


def test_load_config(tmp_path) -> None:
    target = tmp_path / "config.json"
    target.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False), encoding="utf-8")
    assert load_config(target).daily_limit == 20


@pytest.mark.parametrize(
    ("mutator", "expected"),
    [
        (lambda data: data.pop("message"), "缺少必填字段：message"),
        (lambda data: data.update(daily_limit=0), "daily_limit 必须是正整数"),
        (lambda data: data.update(daily_limit=201), "daily_limit 不应超过 200"),
        (lambda data: data.update(session_limit=0), "session_limit 必须是正整数"),
        (lambda data: data.update(session_limit=201), "session_limit 不应超过 200"),
        (lambda data: data.update(interval_seconds=-1), "interval_seconds"),
        (lambda data: data.update(interval_seconds=3601), "interval_seconds"),
        (lambda data: data.update(include_keywords="Python"), "include_keywords 必须是字符串数组"),
        (lambda data: data.update(match_mode="some"), "match_mode"),
        (
            lambda data: data["candidate_filter"].update(cities="杭州"),
            "candidate_filter.cities 必须是字符串数组",
        ),
        (
            lambda data: data["candidate_filter"].update(salary_min_k=20, salary_max_k=10),
            "最低薪资不能高于最高薪资",
        ),
        (
            lambda data: data["auto_reply"].update(per_conversation_limit=0),
            "auto_reply.per_conversation_limit",
        ),
    ],
)
def test_invalid_config(mutator, expected: str) -> None:
    raw = deepcopy(DEFAULT_CONFIG)
    mutator(raw)
    with pytest.raises(ConfigError, match=expected):
        AppConfig.from_dict(raw)


def test_missing_config_is_clear_error(tmp_path) -> None:
    with pytest.raises(ConfigError, match="未找到 config.json"):
        load_config(tmp_path / "config.json")


def test_legacy_config_migrates_session_limit_from_batch_size(tmp_path) -> None:
    raw = deepcopy(DEFAULT_CONFIG)
    raw.pop("session_limit")
    raw["batch_size"] = 7
    target = tmp_path / "config.json"
    target.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    assert load_config(target).session_limit == 7


def test_legacy_config_migrates_greeting_blocked_texts(tmp_path) -> None:
    raw = deepcopy(DEFAULT_CONFIG)
    raw["selectors"].pop("greeting_blocked_texts")
    target = tmp_path / "config.json"
    target.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    assert "去充值" in load_config(target).selectors["greeting_blocked_texts"]


def test_legacy_config_migrates_safe_auto_reply_defaults(tmp_path) -> None:
    raw = deepcopy(DEFAULT_CONFIG)
    raw.pop("auto_reply")
    target = tmp_path / "config.json"
    target.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    config = load_config(target)
    assert config.auto_reply.enabled is False
    assert config.auto_reply.rules == ()


def test_legacy_config_migrates_candidate_filter_from_include_keywords(tmp_path) -> None:
    raw = deepcopy(DEFAULT_CONFIG)
    raw.pop("candidate_filter")
    raw["include_keywords"] = ["销售"]
    target = tmp_path / "config.json"
    target.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    config = load_config(target)

    assert config.candidate_filter == CandidateFilterSettings(keywords=("销售",))


def test_legacy_auto_reply_migrates_new_safety_limits(tmp_path) -> None:
    raw = deepcopy(DEFAULT_CONFIG)
    raw["auto_reply"].pop("per_conversation_limit")
    raw["auto_reply"].pop("interval_seconds")
    target = tmp_path / "config.json"
    target.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    config = load_config(target)

    assert config.auto_reply.per_conversation_limit == 3
    assert config.auto_reply.interval_seconds == 10


def test_legacy_reply_rule_migrates_to_enabled(tmp_path) -> None:
    raw = deepcopy(DEFAULT_CONFIG)
    raw["auto_reply"]["rules"] = [
        {
            "name": "面试时间",
            "keywords": ["明天"],
            "reply": "明天下午方便吗？",
            "match_mode": "any",
        }
    ]
    target = tmp_path / "config.json"
    target.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    config = load_config(target)

    assert config.auto_reply.rules[0].enabled is True


def test_save_runtime_settings_persists_and_preserves_other_fields(tmp_path) -> None:
    target = tmp_path / "config.json"
    target.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False), encoding="utf-8")
    saved = save_runtime_settings(
        target,
        session_limit=12,
        interval_seconds=8,
        daily_limit=80,
        message="新的面试邀约话术",
        dry_run=False,
        auto_reply_enabled=True,
        reply_rules=(ReplyRule("话术1", ("可以", "有兴趣"), "请问明天下午方便吗？"),),
        candidate_filter=CandidateFilterSettings(
            keywords=("销售",),
            cities=("杭州",),
            salary_min_k=10,
            salary_max_k=20,
        ),
        auto_reply_poll_seconds=8,
        auto_reply_daily_limit=15,
        auto_reply_per_conversation_limit=2,
        auto_reply_interval_seconds=12,
        auto_reply_stop_keywords=("不用联系", "已找到工作"),
    )
    raw = json.loads(target.read_text(encoding="utf-8"))
    assert (saved.session_limit, saved.interval_seconds, saved.daily_limit) == (12, 8, 80)
    assert (raw["session_limit"], raw["interval_seconds"], raw["daily_limit"]) == (12, 8, 80)
    assert saved.message == "新的面试邀约话术"
    assert raw["message"] == "新的面试邀约话术"
    assert saved.dry_run is False
    assert raw["dry_run"] is False
    assert saved.auto_reply.enabled is True
    assert saved.auto_reply.rules[0].keywords == ("可以", "有兴趣")
    assert saved.auto_reply.poll_seconds == 8
    assert saved.auto_reply.daily_limit == 15
    assert saved.auto_reply.per_conversation_limit == 2
    assert saved.auto_reply.interval_seconds == 12
    assert saved.auto_reply.stop_keywords == ("不用联系", "已找到工作")
    assert saved.candidate_filter.cities == ("杭州",)
    assert raw["candidate_filter"]["salary_min_k"] == 10
    assert raw["selectors"] == DEFAULT_CONFIG["selectors"]
