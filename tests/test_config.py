from __future__ import annotations

import json
from copy import deepcopy

import pytest

from app.config import DEFAULT_CONFIG, AppConfig, ConfigError, load_config, validate_config


def test_default_config_is_valid_and_safe() -> None:
    config = AppConfig.defaults()
    assert config.control_mode == "desktop"
    assert config.dry_run is True
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
        (lambda data: data.update(interval_seconds=-1), "interval_seconds"),
        (lambda data: data.update(include_keywords="Python"), "include_keywords 必须是字符串数组"),
        (lambda data: data.update(match_mode="some"), "match_mode"),
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
