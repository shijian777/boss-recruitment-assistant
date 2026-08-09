from __future__ import annotations

import pytest

from app.ui_components import parse_optional_positive_int, split_terms


def test_split_terms_accepts_chinese_and_english_separators() -> None:
    assert split_terms("销售，AI, 客户开发 | 销售") == ("销售", "AI", "客户开发")


def test_parse_optional_positive_int_allows_blank() -> None:
    assert parse_optional_positive_int("", field_name="最低薪资") is None
    assert parse_optional_positive_int(" 12 ", field_name="最低薪资") == 12


def test_parse_optional_positive_int_rejects_non_positive_value() -> None:
    with pytest.raises(ValueError, match="最低薪资必须为空或填写正整数"):
        parse_optional_positive_int("0", field_name="最低薪资")
