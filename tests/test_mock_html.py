from pathlib import Path

from app.html_probe import probe_html


FIXTURE = Path(__file__).parent / "fixtures" / "mock_boss.html"


def test_local_mock_page_contains_required_controls() -> None:
    result = probe_html(FIXTURE.read_text(encoding="utf-8"), ["验证码", "请完成验证"])
    assert result.candidate_cards == 2
    assert result.greeting_buttons == 2
    assert result.message_inputs == 1
    assert result.send_buttons == 1
    assert result.next_buttons == 1
    assert result.risk_matches == ("请完成验证",)
