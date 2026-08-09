from __future__ import annotations

from pathlib import Path

from app.config import load_config


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_distributed_config_is_safe_and_has_new_settings() -> None:
    config = load_config(PROJECT_ROOT / "config.example.json")

    assert config.dry_run is True
    assert config.auto_reply.enabled is False
    assert config.candidate_filter.keywords == ()
    assert config.candidate_filter.cities == ()
    assert config.candidate_filter.salary_min_k is None
    assert config.candidate_filter.salary_max_k is None


def test_build_checks_all_release_artifacts() -> None:
    source = (PROJECT_ROOT / "build.bat").read_text(encoding="utf-8")

    assert 'if not exist "dist\\BossInviter\\BossInviter.exe" goto :failed' in source
    assert 'if not exist "dist\\BossInviter-Windows-x64.zip" goto :failed' in source
    assert 'if not exist "dist\\BossInviter-Setup.exe" goto :failed' in source


def test_installer_builder_runs_iexpress_hidden() -> None:
    source = (PROJECT_ROOT / "installer" / "build_installer.ps1").read_text(
        encoding="utf-8"
    )

    assert "-WindowStyle Hidden" in source


def test_readme_documents_simple_pages_and_filter_behavior() -> None:
    source = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")

    for label in ("运行面板", "筛选设置", "自动回复", "数据统计", "运行日志"):
        assert label in source
    assert "关键词、城市、期望薪资" in source
    assert "全部留空" in source
