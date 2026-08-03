from app.candidate import make_candidate_key


def test_candidate_id_has_highest_priority() -> None:
    first = make_candidate_key(stable_id="Geek-123", fallback_text="在线")
    second = make_candidate_key(stable_id="geek-123", fallback_text="3 小时前活跃")
    assert first == second
    assert first.startswith("id:")


def test_candidate_url_ignores_tracking_parameters() -> None:
    first = make_candidate_key(candidate_url="https://example.test/geek/detail?id=42&utm_source=a")
    second = make_candidate_key(candidate_url="https://example.test/geek/detail?utm_source=b&id=42")
    assert first == second


def test_stable_fields_ignore_fallback_text_changes() -> None:
    first = make_candidate_key(stable_fields=["张某", "Python工程师", "示例科技"], fallback_text="刚刚活跃")
    second = make_candidate_key(stable_fields=["张某", "Python工程师", "示例科技"], fallback_text="昨天活跃")
    assert first == second
    assert first.startswith("fields:")
