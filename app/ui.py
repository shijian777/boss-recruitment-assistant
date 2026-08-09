from __future__ import annotations

import os
import queue
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any, Callable

from app.automation import AutomationController
from app.candidate_filter import CandidateFilterSettings
from app.config import AppConfig, ConfigError, save_runtime_settings
from app.database import Database
from app.reply_rules import (
    ReplyLibraryError,
    ReplyRule,
    choose_reply,
    validate_reply_rules,
)
from app.ui_components import (
    CollapsibleSection,
    ReplyRuleDialog,
    parse_optional_positive_int,
    split_terms,
)


class BossInviterApp:
    _ACCENT = "#0aa79d"
    _SIDEBAR = "#083f46"
    _SIDEBAR_ACTIVE = "#126f75"

    def __init__(
        self,
        root: tk.Tk,
        *,
        config: AppConfig | None,
        config_error: str,
        database: Database | None,
        base_dir: Path,
        log_dir: Path,
        screenshot_dir: Path,
        event_queue: "queue.Queue[dict[str, object]]",
        logger: Any,
        on_closed: Callable[[], None] | None = None,
    ) -> None:
        self.root = root
        self.config = config
        self.config_error = config_error
        self.database = database
        self.base_dir = base_dir
        self.log_dir = log_dir
        self.screenshot_dir = screenshot_dir
        self.event_queue = event_queue
        self.logger = logger
        self.on_closed = on_closed
        self.controller: AutomationController | None = None
        self._closing = False
        self._formal_confirmed = False
        self.action_buttons: list[ttk.Button] = []
        self.pages: dict[str, ttk.Frame] = {}
        self.nav_buttons: dict[str, tk.Button] = {}
        self._reply_rules = list(config.auto_reply.rules) if config else []

        self.root.title("BossInviter · BOSS 直聘邀约助手")
        self.root.geometry("1180x800")
        self.root.minsize(1000, 700)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.option_add("*Font", ("Microsoft YaHei UI", 10))

        style = ttk.Style(self.root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 19, "bold"))
        style.configure("Subtitle.TLabel", foreground="#64757d")
        style.configure("CardValue.TLabel", font=("Microsoft YaHei UI", 20, "bold"))
        style.configure("Primary.TButton", background=self._ACCENT, foreground="white")
        style.map("Primary.TButton", background=[("active", "#078b83")])

        self.mode_var = tk.StringVar(
            value=(
                "配置错误"
                if config is None
                else ("测试模式" if config.dry_run else "正式发送模式")
            )
        )
        self.formal_mode_enabled_var = tk.BooleanVar(
            value=(not config.dry_run) if config else False
        )
        self.state_var = tk.StringVar(value="配置错误" if config is None else "未启动")
        self.contacted_var = tk.StringVar(value="0")
        self.today_var = tk.StringVar(value="0")
        self.unverified_var = tk.StringVar(value="0")
        self.messages_var = tk.StringVar(value="--")
        self.replied_var = tk.StringVar(value="0")
        self.limit_var = tk.StringVar(value=str(config.daily_limit) if config else "-")
        self.candidate_var = tk.StringVar(value="-")
        self.result_var = tk.StringVar(value="-")
        self.success_var = tk.StringVar(value="0")
        self.skipped_var = tk.StringVar(value="0")
        self.failed_var = tk.StringVar(value="0")
        self.session_limit_input_var = tk.StringVar(
            value=str(config.session_limit) if config else ""
        )
        self.interval_input_var = tk.StringVar(
            value=str(config.interval_seconds) if config else ""
        )
        self.daily_limit_input_var = tk.StringVar(
            value=str(config.daily_limit) if config else ""
        )
        self.settings_status_var = tk.StringVar(value="")
        self.filter_status_var = tk.StringVar(value="")
        self.auto_reply_enabled_var = tk.BooleanVar(
            value=config.auto_reply.enabled if config else False
        )
        candidate_filter = config.candidate_filter if config else CandidateFilterSettings()
        self.filter_keywords_var = tk.StringVar(value="，".join(candidate_filter.keywords))
        self.filter_cities_var = tk.StringVar(value="，".join(candidate_filter.cities))
        self.salary_min_var = tk.StringVar(
            value="" if candidate_filter.salary_min_k is None else str(candidate_filter.salary_min_k)
        )
        self.salary_max_var = tk.StringVar(
            value="" if candidate_filter.salary_max_k is None else str(candidate_filter.salary_max_k)
        )
        self.reply_poll_var = tk.StringVar(
            value=str(config.auto_reply.poll_seconds) if config else "10"
        )
        self.reply_daily_limit_var = tk.StringVar(
            value=str(config.auto_reply.daily_limit) if config else "20"
        )
        self.reply_conversation_limit_var = tk.StringVar(
            value=str(config.auto_reply.per_conversation_limit) if config else "3"
        )
        self.reply_interval_var = tk.StringVar(
            value=str(config.auto_reply.interval_seconds) if config else "10"
        )
        self.reply_stop_words_var = tk.StringVar(
            value="，".join(config.auto_reply.stop_keywords) if config else ""
        )
        self.reply_test_result_var = tk.StringVar(value="尚未测试")

        self._build_widgets()
        if config is not None and database is not None:
            stats = database.dashboard_stats()
            self.contacted_var.set(str(stats.contacted))
            self.today_var.set(str(stats.sent))
            self.unverified_var.set(str(stats.unverified))
            self.replied_var.set(str(stats.replied))
            self.controller = AutomationController(
                config,
                database,
                base_dir,
                screenshot_dir,
                event_queue,
                logger,
            )
        else:
            self._set_controls_enabled(False)
            self._append_log(config_error)
        self.root.after(100, self._poll_events)
        if self.controller is not None and self.config is not None and self.config.auto_start:
            self.root.after(700, self._auto_start)

    def _build_widgets(self) -> None:
        shell = tk.Frame(self.root, bg="#eef3f5")
        shell.pack(fill="both", expand=True)

        sidebar = tk.Frame(shell, width=190, bg=self._SIDEBAR)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        tk.Label(
            sidebar,
            text="BossInviter",
            bg=self._SIDEBAR,
            fg="white",
            font=("Microsoft YaHei UI", 17, "bold"),
            anchor="w",
            padx=22,
            pady=24,
        ).pack(fill="x")

        navigation = (
            ("run", "运行面板"),
            ("filter", "筛选设置"),
            ("reply", "自动回复"),
            ("stats", "数据统计"),
            ("logs", "运行日志"),
        )
        for name, label in navigation:
            button = tk.Button(
                sidebar,
                text=label,
                command=lambda page=name: self._show_page(page),
                bg=self._SIDEBAR,
                fg="#dbeaec",
                activebackground=self._SIDEBAR_ACTIVE,
                activeforeground="white",
                relief="flat",
                bd=0,
                anchor="w",
                padx=24,
                pady=13,
                cursor="hand2",
            )
            button.pack(fill="x", padx=10, pady=2)
            self.nav_buttons[name] = button

        tk.Label(
            sidebar,
            text="本地运行 · 数据不上传",
            bg=self._SIDEBAR,
            fg="#8eb6ba",
            anchor="w",
            padx=20,
            pady=18,
        ).pack(side="bottom", fill="x")

        content = ttk.Frame(shell, padding=20)
        content.pack(side="left", fill="both", expand=True)
        content.rowconfigure(0, weight=1)
        content.columnconfigure(0, weight=1)
        for name, _label in navigation:
            page = ttk.Frame(content)
            page.grid(row=0, column=0, sticky="nsew")
            self.pages[name] = page

        self._build_run_page(self.pages["run"])
        self._build_filter_page(self.pages["filter"])
        self._build_reply_page(self.pages["reply"])
        self._build_stats_page(self.pages["stats"])
        self._build_logs_page(self.pages["logs"])
        self._refresh_rule_tree()
        self._show_page("run")

    @staticmethod
    def _page_header(page: ttk.Frame, title: str, subtitle: str) -> None:
        ttk.Label(page, text=title, style="Title.TLabel").pack(anchor="w")
        ttk.Label(page, text=subtitle, style="Subtitle.TLabel").pack(
            anchor="w", pady=(3, 16)
        )

    def _show_page(self, name: str) -> None:
        self.pages[name].tkraise()
        for key, button in self.nav_buttons.items():
            active = key == name
            button.configure(
                bg=self._SIDEBAR_ACTIVE if active else self._SIDEBAR,
                fg="white" if active else "#dbeaec",
                font=("Microsoft YaHei UI", 10, "bold" if active else "normal"),
            )

    def _stat_cards(self, parent: ttk.Frame, fields: tuple[tuple[str, tk.StringVar], ...]) -> ttk.Frame:
        frame = ttk.Frame(parent)
        for index, (label, variable) in enumerate(fields):
            card = ttk.LabelFrame(frame, text=label, padding=(14, 9))
            card.grid(row=0, column=index, padx=(0, 10), sticky="nsew")
            ttk.Label(card, textvariable=variable, style="CardValue.TLabel").pack(anchor="w")
            frame.columnconfigure(index, weight=1)
        return frame

    def _build_run_page(self, page: ttk.Frame) -> None:
        self._page_header(page, "运行面板", "设置本次任务后，连接 BOSS 客户端并开始运行。")

        status = ttk.Frame(page)
        status.pack(fill="x", pady=(0, 12))
        ttk.Label(status, text="模式：").pack(side="left")
        ttk.Label(status, textvariable=self.mode_var, foreground="#087f5b").pack(side="left")
        ttk.Label(status, text="    状态：").pack(side="left")
        ttk.Label(status, textvariable=self.state_var).pack(side="left")

        cards = self._stat_cards(
            page,
            (
                ("今日沟通", self.contacted_var),
                ("成功发送", self.today_var),
                ("自动回复", self.replied_var),
                ("待人工核对", self.unverified_var),
            ),
        )
        cards.pack(fill="x")

        settings = ttk.LabelFrame(page, text="本次任务", padding=14)
        settings.pack(fill="x", pady=(14, 0))
        setting_fields = (
            ("本次人数", self.session_limit_input_var, "人"),
            ("发送间隔", self.interval_input_var, "秒"),
            ("每日上限", self.daily_limit_input_var, "人"),
        )
        for column, (label, variable, unit) in enumerate(setting_fields):
            group = ttk.Frame(settings)
            group.grid(row=0, column=column, padx=(0, 26), sticky="w")
            ttk.Label(group, text=label).pack(anchor="w")
            row = ttk.Frame(group)
            row.pack(anchor="w", pady=(5, 0))
            ttk.Entry(row, textvariable=variable, width=10).pack(side="left")
            ttk.Label(row, text=unit).pack(side="left", padx=(5, 0))
        save_button = ttk.Button(
            settings,
            text="保存全部设置",
            style="Primary.TButton",
            command=self._save_settings,
        )
        save_button.grid(row=0, column=3, sticky="e")
        settings.columnconfigure(3, weight=1)
        self.action_buttons.append(save_button)
        ttk.Checkbutton(
            settings,
            text="正式发送（关闭时为安全测试模式）",
            variable=self.formal_mode_enabled_var,
        ).grid(row=1, column=0, columnspan=4, sticky="w", pady=(10, 0))
        ttk.Label(
            settings,
            textvariable=self.settings_status_var,
            foreground="#087f5b",
        ).grid(row=2, column=0, columnspan=4, sticky="w", pady=(7, 0))

        controls = ttk.Frame(page)
        controls.pack(fill="x", pady=14)
        actions = (
            ("连接客户端", self._connect_client, ""),
            ("开始/继续", self._resume, "Primary.TButton"),
            ("暂停", self._pause, ""),
            ("停止", self._stop, ""),
            ("页面诊断", self._diagnose, ""),
        )
        for text, command, style_name in actions:
            options: dict[str, object] = {"text": text, "command": command}
            if style_name:
                options["style"] = style_name
            button = ttk.Button(controls, **options)
            button.pack(side="left", padx=(0, 8))
            self.action_buttons.append(button)

        current = ttk.LabelFrame(page, text="当前候选人", padding=12)
        current.pack(fill="x")
        ttk.Label(current, textvariable=self.candidate_var).pack(anchor="w")
        ttk.Label(current, textvariable=self.result_var, foreground="#607078").pack(
            anchor="w", pady=(4, 0)
        )

        manual = ttk.LabelFrame(page, text="手动回复当前会话", padding=12)
        manual.pack(fill="both", expand=True, pady=(14, 0))
        self.message_text = tk.Text(manual, height=3, wrap="word")
        self.message_text.pack(fill="x")
        self.message_text.insert("1.0", self.config.message if self.config else "")
        manual_controls = ttk.Frame(manual)
        manual_controls.pack(fill="x", pady=(8, 0))
        ttk.Label(
            manual_controls,
            text="先在 BOSS 中手动打开目标聊天；测试模式不会发送。",
            foreground="#607078",
        ).pack(side="left")
        reply_button = ttk.Button(
            manual_controls,
            text="回复当前会话",
            command=self._reply_current_chat,
        )
        reply_button.pack(side="right")
        self.action_buttons.append(reply_button)

    def _build_filter_page(self, page: ttk.Frame) -> None:
        self._page_header(
            page,
            "候选人筛选设置",
            "三个条件都是可选项；全部留空时不筛选，直接执行自动打招呼。",
        )
        summary = ttk.LabelFrame(page, text="当前规则", padding=14)
        summary.pack(fill="x")
        ttk.Label(
            summary,
            text="已填写的类别必须同时满足；关键词和城市各自命中任意一个即可。",
            foreground="#087f5b",
        ).pack(anchor="w")

        form = ttk.LabelFrame(page, text="筛选条件", padding=16)
        form.pack(fill="x", pady=(14, 0))
        for column in range(3):
            form.columnconfigure(column, weight=1)

        keyword = ttk.Frame(form)
        keyword.grid(row=0, column=0, padx=(0, 14), sticky="nsew")
        ttk.Label(keyword, text="关键词").pack(anchor="w")
        ttk.Entry(keyword, textvariable=self.filter_keywords_var).pack(
            fill="x", pady=(7, 4)
        )
        ttk.Label(keyword, text="例如：销售，AI，客户开发", foreground="#718087").pack(anchor="w")

        city = ttk.Frame(form)
        city.grid(row=0, column=1, padx=(0, 14), sticky="nsew")
        ttk.Label(city, text="城市").pack(anchor="w")
        ttk.Entry(city, textvariable=self.filter_cities_var).pack(
            fill="x", pady=(7, 4)
        )
        ttk.Label(city, text="例如：杭州，上海", foreground="#718087").pack(anchor="w")

        salary = ttk.Frame(form)
        salary.grid(row=0, column=2, sticky="nsew")
        ttk.Label(salary, text="期望薪资范围（K/月）").pack(anchor="w")
        salary_row = ttk.Frame(salary)
        salary_row.pack(fill="x", pady=(7, 4))
        ttk.Entry(salary_row, textvariable=self.salary_min_var, width=9).pack(side="left", fill="x", expand=True)
        ttk.Label(salary_row, text="  至  ").pack(side="left")
        ttk.Entry(salary_row, textvariable=self.salary_max_var, width=9).pack(side="left", fill="x", expand=True)
        ttk.Label(salary, text="留空表示不限制", foreground="#718087").pack(anchor="w")

        footer = ttk.Frame(page)
        footer.pack(fill="x", pady=14)
        ttk.Label(footer, textvariable=self.filter_status_var, foreground="#087f5b").pack(side="left")
        clear_button = ttk.Button(footer, text="清空筛选", command=self._clear_filters)
        clear_button.pack(side="right")
        save_button = ttk.Button(
            footer,
            text="保存筛选设置",
            style="Primary.TButton",
            command=self._save_settings,
        )
        save_button.pack(side="right", padx=(0, 8))
        self.action_buttons.extend((clear_button, save_button))

    def _build_reply_page(self, page: ttk.Frame) -> None:
        self._page_header(
            page,
            "自动回复",
            "只发送话术库中已审核的固定回复；未命中或冲突时转人工。",
        )

        switch = ttk.LabelFrame(page, text="自动回复状态", padding=13)
        switch.pack(fill="x")
        ttk.Checkbutton(
            switch,
            text="允许自动回复",
            variable=self.auto_reply_enabled_var,
        ).pack(side="left")
        ttk.Label(
            switch,
            text="保存后仍需本人点击“开启自动回复”才会开始检测未读消息。",
            foreground="#607078",
        ).pack(side="left", padx=(14, 0))
        start_button = ttk.Button(
            switch,
            text="开启自动回复",
            style="Primary.TButton",
            command=self._start_auto_reply,
        )
        start_button.pack(side="right")
        self.action_buttons.append(start_button)

        library = ttk.LabelFrame(page, text="我的话术", padding=12)
        library.pack(fill="both", expand=True, pady=(14, 0))
        columns = ("keywords", "reply", "status")
        self.reply_rule_tree = ttk.Treeview(
            library,
            columns=columns,
            show="headings",
            height=8,
            selectmode="browse",
        )
        self.reply_rule_tree.heading("keywords", text="触发词")
        self.reply_rule_tree.heading("reply", text="固定回复")
        self.reply_rule_tree.heading("status", text="状态")
        self.reply_rule_tree.column("keywords", width=220, anchor="w")
        self.reply_rule_tree.column("reply", width=470, anchor="w")
        self.reply_rule_tree.column("status", width=70, anchor="center")
        self.reply_rule_tree.pack(fill="both", expand=True)
        self.reply_rule_tree.bind("<Double-1>", lambda _event: self._edit_rule())

        rule_actions = ttk.Frame(library)
        rule_actions.pack(fill="x", pady=(9, 0))
        for text, command in (
            ("新增话术", self._add_rule),
            ("编辑", self._edit_rule),
            ("删除", self._delete_rule),
        ):
            button = ttk.Button(rule_actions, text=text, command=command)
            button.pack(side="left", padx=(0, 7))
            self.action_buttons.append(button)
        save_button = ttk.Button(
            rule_actions,
            text="保存话术库",
            style="Primary.TButton",
            command=self._save_settings,
        )
        save_button.pack(side="right")
        self.action_buttons.append(save_button)

        advanced = CollapsibleSection(page, "防误发与次数限制")
        advanced.pack(fill="x", pady=(12, 0))
        limit_fields = (
            ("检查未读间隔（秒）", self.reply_poll_var),
            ("每日回复上限", self.reply_daily_limit_var),
            ("单会话上限", self.reply_conversation_limit_var),
            ("两次回复间隔（秒）", self.reply_interval_var),
        )
        for column, (label, variable) in enumerate(limit_fields):
            group = ttk.Frame(advanced.body)
            group.grid(row=0, column=column, padx=(0, 14), sticky="ew")
            ttk.Label(group, text=label).pack(anchor="w")
            ttk.Entry(group, textvariable=variable, width=12).pack(fill="x", pady=(4, 0))
            advanced.body.columnconfigure(column, weight=1)
        ttk.Label(advanced.body, text="停止联系词").grid(row=1, column=0, sticky="w", pady=(10, 0))
        ttk.Entry(advanced.body, textvariable=self.reply_stop_words_var).grid(
            row=2, column=0, columnspan=4, sticky="ew", pady=(4, 0)
        )

        tester = CollapsibleSection(page, "测试一条来信")
        tester.pack(fill="x", pady=(8, 0))
        self.reply_test_text = tk.Text(tester.body, height=3, wrap="word")
        self.reply_test_text.pack(fill="x")
        test_footer = ttk.Frame(tester.body)
        test_footer.pack(fill="x", pady=(7, 0))
        ttk.Label(test_footer, textvariable=self.reply_test_result_var).pack(side="left")
        ttk.Button(test_footer, text="测试匹配", command=self._test_reply_match).pack(side="right")

    def _build_stats_page(self, page: ttk.Frame) -> None:
        self._page_header(page, "数据统计", "显示今天由本程序记录的沟通和回复结果。")
        first = self._stat_cards(
            page,
            (
                ("今日沟通", self.contacted_var),
                ("成功发送", self.today_var),
                ("自动回复", self.replied_var),
                ("待人工核对", self.unverified_var),
            ),
        )
        first.pack(fill="x")
        second = self._stat_cards(
            page,
            (
                ("当前未读", self.messages_var),
                ("本次成功/匹配", self.success_var),
                ("本次跳过", self.skipped_var),
                ("本次失败", self.failed_var),
            ),
        )
        second.pack(fill="x", pady=(12, 0))
        refresh = ttk.Button(page, text="刷新统计", command=self._refresh_stats)
        refresh.pack(anchor="e", pady=14)
        self.action_buttons.append(refresh)

    def _build_logs_page(self, page: ttk.Frame) -> None:
        self._page_header(page, "运行日志", "普通原因直接显示；需要排查时可打开日志或截图目录。")
        toolbar = ttk.Frame(page)
        toolbar.pack(fill="x", pady=(0, 9))
        ttk.Button(toolbar, text="打开日志目录", command=lambda: self._open_directory(self.log_dir)).pack(side="left")
        ttk.Button(toolbar, text="打开截图目录", command=lambda: self._open_directory(self.screenshot_dir)).pack(side="left", padx=(8, 0))
        log_frame = ttk.Frame(page)
        log_frame.pack(fill="both", expand=True)
        scrollbar = ttk.Scrollbar(log_frame)
        scrollbar.pack(side="right", fill="y")
        self.log_text = tk.Text(
            log_frame,
            wrap="word",
            state="disabled",
            yscrollcommand=scrollbar.set,
        )
        self.log_text.pack(fill="both", expand=True)
        scrollbar.configure(command=self.log_text.yview)

    def _set_controls_enabled(self, enabled: bool) -> None:
        state = "normal" if enabled else "disabled"
        for button in self.action_buttons:
            button.configure(state=state)

    def _append_log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message.rstrip() + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _open_directory(self, path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(path)  # type: ignore[attr-defined]
        except OSError as exc:
            messagebox.showerror("无法打开目录", str(exc), parent=self.root)

    def _connect_client(self) -> None:
        if self.controller:
            self.state_var.set("正在连接 BOSS 客户端…")
            self.controller.connect_client()

    def _clear_filters(self) -> None:
        self.filter_keywords_var.set("")
        self.filter_cities_var.set("")
        self.salary_min_var.set("")
        self.salary_max_var.set("")
        self.filter_status_var.set("筛选条件已清空；点击保存后将不筛选。")

    def _refresh_rule_tree(self) -> None:
        if not hasattr(self, "reply_rule_tree"):
            return
        for item in self.reply_rule_tree.get_children():
            self.reply_rule_tree.delete(item)
        for index, rule in enumerate(self._reply_rules):
            reply_preview = rule.reply if len(rule.reply) <= 55 else f"{rule.reply[:55]}…"
            self.reply_rule_tree.insert(
                "",
                "end",
                iid=str(index),
                values=("、".join(rule.keywords), reply_preview, "启用" if rule.enabled else "停用"),
            )

    def _selected_rule_index(self) -> int | None:
        selection = self.reply_rule_tree.selection()
        if not selection:
            messagebox.showinfo("请选择话术", "请先在列表中选择一条话术。", parent=self.root)
            return None
        return int(selection[0])

    def _add_rule(self) -> None:
        dialog = ReplyRuleDialog(self.root)
        self.root.wait_window(dialog)
        if dialog.result is None:
            return
        candidate = [*self._reply_rules, dialog.result]
        try:
            validate_reply_rules(candidate)
        except ReplyLibraryError as exc:
            messagebox.showerror("话术冲突", str(exc), parent=self.root)
            return
        self._reply_rules = candidate
        self._refresh_rule_tree()

    def _edit_rule(self) -> None:
        index = self._selected_rule_index()
        if index is None:
            return
        dialog = ReplyRuleDialog(self.root, self._reply_rules[index])
        self.root.wait_window(dialog)
        if dialog.result is None:
            return
        candidate = list(self._reply_rules)
        candidate[index] = dialog.result
        try:
            validate_reply_rules(candidate)
        except ReplyLibraryError as exc:
            messagebox.showerror("话术冲突", str(exc), parent=self.root)
            return
        self._reply_rules = candidate
        self._refresh_rule_tree()
        self.reply_rule_tree.selection_set(str(index))

    def _delete_rule(self) -> None:
        index = self._selected_rule_index()
        if index is None:
            return
        rule = self._reply_rules[index]
        if not messagebox.askyesno(
            "删除话术",
            f"确认删除“{rule.name}”吗？",
            parent=self.root,
        ):
            return
        del self._reply_rules[index]
        self._refresh_rule_tree()

    def _test_reply_match(self) -> None:
        incoming = self.reply_test_text.get("1.0", "end").strip()
        if not incoming:
            self.reply_test_result_var.set("请先输入一条模拟来信。")
            return
        stop_words = split_terms(self.reply_stop_words_var.get())
        decision = choose_reply(incoming, tuple(self._reply_rules), stop_words)
        if decision.action == "reply":
            self.reply_test_result_var.set(
                f"命中“{decision.rule_name}”：{decision.reply}"
            )
        elif decision.action == "stop":
            self.reply_test_result_var.set(f"停止自动回复：{decision.reason}")
        else:
            self.reply_test_result_var.set(f"转人工：{decision.reason}")

    def _save_settings(self) -> None:
        if self.config is None or self.controller is None:
            return
        desired_dry_run = not self.formal_mode_enabled_var.get()
        if self.config.dry_run and not desired_dry_run:
            confirmed = messagebox.askyesno(
                "开启正式发送模式",
                "正式发送模式会在 BOSS 中真实执行打招呼和已允许的回复。\n\n"
                "仍会受本次人数、发送间隔和每日上限约束。\n"
                "确认开启并保存吗？",
                parent=self.root,
            )
            if not confirmed:
                self.formal_mode_enabled_var.set(False)
                return
        try:
            session_limit = int(self.session_limit_input_var.get().strip())
            interval_seconds = int(self.interval_input_var.get().strip())
            daily_limit = int(self.daily_limit_input_var.get().strip())
            salary_minimum = parse_optional_positive_int(
                self.salary_min_var.get(), field_name="最低薪资"
            )
            salary_maximum = parse_optional_positive_int(
                self.salary_max_var.get(), field_name="最高薪资"
            )
            reply_poll_seconds = int(self.reply_poll_var.get().strip())
            reply_daily_limit = int(self.reply_daily_limit_var.get().strip())
            reply_conversation_limit = int(
                self.reply_conversation_limit_var.get().strip()
            )
            reply_interval_seconds = int(self.reply_interval_var.get().strip())
        except ValueError as exc:
            messagebox.showerror(
                "设置错误",
                str(exc) if str(exc) else "人数、间隔和限制必须填写整数。",
                parent=self.root,
            )
            return

        try:
            message = self.message_text.get("1.0", "end").strip()
            reply_rules = tuple(self._reply_rules)
            validate_reply_rules(reply_rules)
            enabled_rules = [rule for rule in reply_rules if rule.enabled]
            if self.auto_reply_enabled_var.get() and not enabled_rules:
                raise ReplyLibraryError("允许自动回复时，至少需要一条已启用话术")
            candidate_filter = CandidateFilterSettings(
                keywords=split_terms(self.filter_keywords_var.get()),
                cities=split_terms(self.filter_cities_var.get()),
                salary_min_k=salary_minimum,
                salary_max_k=salary_maximum,
            )
            stop_words = split_terms(self.reply_stop_words_var.get())
            updated = save_runtime_settings(
                self.base_dir / "config.json",
                session_limit=session_limit,
                interval_seconds=interval_seconds,
                daily_limit=daily_limit,
                message=message,
                dry_run=desired_dry_run,
                auto_reply_enabled=self.auto_reply_enabled_var.get(),
                reply_rules=reply_rules,
                candidate_filter=candidate_filter,
                auto_reply_poll_seconds=reply_poll_seconds,
                auto_reply_daily_limit=reply_daily_limit,
                auto_reply_per_conversation_limit=reply_conversation_limit,
                auto_reply_interval_seconds=reply_interval_seconds,
                auto_reply_stop_keywords=stop_words,
            )
        except (ConfigError, ReplyLibraryError) as exc:
            messagebox.showerror("设置错误", str(exc), parent=self.root)
            return

        self.config = updated
        self.controller.update_config(updated)
        self.limit_var.set(str(updated.daily_limit))
        self.mode_var.set("测试模式" if updated.dry_run else "正式发送模式")
        self._formal_confirmed = False
        self.settings_status_var.set("设置已保存，从下一位候选人或下一次来信起生效。")
        if not any(
            (
                updated.candidate_filter.keywords,
                updated.candidate_filter.cities,
                updated.candidate_filter.salary_min_k is not None,
                updated.candidate_filter.salary_max_k is not None,
            )
        ):
            self.filter_status_var.set("已保存：不筛选，直接执行自动打招呼。")
        else:
            self.filter_status_var.set("已保存：按已填写的关键词、城市和薪资同时筛选。")
        self._append_log(
            f"设置已保存：本次 {updated.session_limit} 人，间隔 {updated.interval_seconds} 秒，"
            f"每日上限 {updated.daily_limit} 人；话术 {len(updated.auto_reply.rules)} 条。"
        )

    def _start_auto_reply(self) -> None:
        if self.controller is None or self.config is None:
            return
        current_rules = tuple(self._reply_rules)
        try:
            validate_reply_rules(current_rules)
        except ReplyLibraryError as exc:
            messagebox.showerror("话术库错误", str(exc), parent=self.root)
            return
        if not self.config.auto_reply.enabled:
            messagebox.showwarning(
                "自动回复未允许",
                "请先打开“允许自动回复”，再保存设置。",
                parent=self.root,
            )
            return
        if current_rules != self.config.auto_reply.rules:
            messagebox.showwarning(
                "话术库尚未保存",
                "话术库内容已变化，请先保存话术库。",
                parent=self.root,
            )
            return
        enabled_rules = [rule for rule in current_rules if rule.enabled]
        if not enabled_rules:
            messagebox.showwarning("话术库为空", "请至少启用一条话术并保存。", parent=self.root)
            return
        if not self.config.dry_run:
            confirmed = messagebox.askyesno(
                "确认开启自动回复",
                "程序将检测所有可识别的未读会话，只发送唯一命中的固定话术。\n"
                "停止词、冲突、方向不明和页面异常都会转人工。\n\n"
                f"启用话术：{len(enabled_rules)} 条\n"
                f"检查间隔：{self.config.auto_reply.poll_seconds} 秒\n"
                f"今日上限：{self.config.auto_reply.daily_limit} 条\n\n"
                "确认开启吗？",
                parent=self.root,
            )
            if not confirmed:
                return
        self.controller.watch_inbox()

    def _reply_current_chat(self) -> None:
        if self.controller is None or self.config is None:
            return
        message = self.message_text.get("1.0", "end").strip()
        if not message:
            messagebox.showerror("话术为空", "请先填写并保存回复话术。", parent=self.root)
            return
        if message != self.config.message:
            messagebox.showwarning("话术尚未保存", "请先保存全部设置。", parent=self.root)
            return
        if not self.config.dry_run:
            confirmed = messagebox.askyesno(
                "确认回复当前会话",
                f"程序只回复当前已经打开的聊天会话。\n\n将发送：\n{message}\n\n确认发送吗？",
                parent=self.root,
            )
            if not confirmed:
                return
        self.controller.reply_current_chat(message)

    def _auto_start(self) -> None:
        if self._closing or not self.controller or not self.config:
            return
        self._append_log("已启用自动启动：正在连接 BOSS 客户端并准备运行。")
        self.controller.connect_client()
        self.root.after(900, self._resume)

    def _diagnose(self) -> None:
        if self.controller:
            self.controller.diagnose()

    def _refresh_stats(self) -> None:
        if self.controller:
            self.controller.refresh_stats()

    def _resume(self) -> None:
        if not self.controller or not self.config:
            return
        if not self.config.dry_run and not self._formal_confirmed:
            confirmed = messagebox.askyesno(
                "确认正式发送",
                "继续后可能真实发送 BOSS 原生招呼。\n\n"
                f"本次人数：{self.config.session_limit}\n"
                f"发送间隔：{self.config.interval_seconds} 秒\n"
                f"每日上限：{self.config.daily_limit}\n\n"
                "确认继续吗？",
                parent=self.root,
            )
            if not confirmed:
                return
            self._formal_confirmed = True
        self.state_var.set("正在进入推荐页并扫描候选人…")
        self.controller.resume()

    def _pause(self) -> None:
        if self.controller:
            self.controller.pause()

    def _stop(self) -> None:
        if self.controller:
            self.controller.stop()

    def _poll_events(self) -> None:
        if self._closing:
            return
        try:
            while True:
                event = self.event_queue.get_nowait()
                event_type = event.get("type")
                if event_type == "log":
                    self._append_log(str(event.get("message", "")))
                elif event_type == "state":
                    self.state_var.set(str(event.get("state", "")))
                elif event_type == "candidate":
                    self.candidate_var.set(str(event.get("name", "")))
                    self.result_var.set(str(event.get("summary", "")))
                elif event_type == "candidate_result":
                    self.result_var.set(str(event.get("result", "")))
                elif event_type == "counts":
                    self.contacted_var.set(str(event.get("today_contacted", 0)))
                    self.today_var.set(str(event.get("today_sent", 0)))
                    self.unverified_var.set(str(event.get("today_unverified", 0)))
                    unread = event.get("unread_messages")
                    self.messages_var.set("--" if unread is None else str(unread))
                    self.replied_var.set(str(event.get("today_replied", 0)))
                    self.success_var.set(str(event.get("success", 0)))
                    self.skipped_var.set(str(event.get("skipped", 0)))
                    self.failed_var.set(str(event.get("failed", 0)))
                elif event_type == "diagnosis":
                    result = event.get("result", {})
                    if isinstance(result, dict):
                        count = result.get("recognized_candidate_count", 0)
                        self.state_var.set(f"诊断完成：识别 {count} 张候选人卡片")
                elif event_type == "worker_stopped" and self.state_var.get() == "正在停止":
                    self.state_var.set("已停止")
        except queue.Empty:
            pass
        self.root.after(100, self._poll_events)

    def _on_close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self._set_controls_enabled(False)
        clean = True
        if self.controller is not None:
            clean = self.controller.shutdown(timeout=5.0)
        if not clean:
            self.logger.error("自动化线程未在 5 秒内退出；进程结束时守护线程会被终止")
        if self.on_closed is not None:
            self.on_closed()
        self.root.destroy()
