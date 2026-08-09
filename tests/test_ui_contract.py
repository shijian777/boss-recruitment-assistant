from __future__ import annotations

import logging
import queue
import tkinter as tk

from app.config import AppConfig
from app.database import Database
from app.reply_rules import ReplyRule
from app.ui import BossInviterApp


def test_sidebar_buttons_switch_pages_and_simple_actions_work(tmp_path) -> None:
    root = tk.Tk()
    root.withdraw()
    events: "queue.Queue[dict[str, object]]" = queue.Queue()
    app = BossInviterApp(
        root,
        config=AppConfig.defaults(),
        config_error="",
        database=Database(tmp_path / "ui.db"),
        base_dir=tmp_path,
        log_dir=tmp_path / "logs",
        screenshot_dir=tmp_path / "screenshots",
        event_queue=events,
        logger=logging.getLogger("ui-contract"),
    )
    try:
        assert set(app.pages) == {"run", "filter", "reply", "stats", "logs"}
        for name, button in app.nav_buttons.items():
            button.invoke()
            root.update_idletasks()
            assert button.cget("bg") == app._SIDEBAR_ACTIVE
            assert app.pages[name].winfo_exists() == 1

        app.filter_keywords_var.set("销售")
        app.filter_cities_var.set("杭州")
        app.salary_min_var.set("10")
        app.salary_max_var.set("20")
        app._clear_filters()
        assert app.filter_keywords_var.get() == ""
        assert app.filter_cities_var.get() == ""
        assert app.salary_min_var.get() == ""
        assert app.salary_max_var.get() == ""

        app._reply_rules = [ReplyRule("意向", ("有兴趣",), "感谢回复")]
        app._refresh_rule_tree()
        app.reply_test_text.insert("1.0", "我有兴趣")
        app._test_reply_match()
        assert "感谢回复" in app.reply_test_result_var.get()

        assert app.formal_mode_enabled_var.get() is False
        assert app.mode_var.get() == "测试模式"
    finally:
        app._on_close()
