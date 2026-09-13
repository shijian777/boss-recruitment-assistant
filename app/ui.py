from __future__ import annotations

import os
import queue
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk
from typing import Any, Callable

from app.automation import AutomationController
from app.config import AppConfig
from app.database import Database


class BossInviterApp:
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

        self.root.title("BOSS 直聘助手")
        self.root.geometry("940x700")
        self.root.minsize(820, 620)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.mode_var = tk.StringVar(value="配置错误" if config is None else ("测试模式" if config.dry_run else "正式发送模式"))
        self.state_var = tk.StringVar(value="配置错误" if config is None else "未启动")
        self.today_var = tk.StringVar(value="0")
        self.limit_var = tk.StringVar(value=str(config.daily_limit) if config else "-")
        self.candidate_var = tk.StringVar(value="-")
        self.result_var = tk.StringVar(value="-")
        self.success_var = tk.StringVar(value="0")
        self.skipped_var = tk.StringVar(value="0")
        self.failed_var = tk.StringVar(value="0")

        self._build_widgets()
        if config is not None and database is not None:
            self.today_var.set(str(database.today_sent_count()))
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

    def _build_widgets(self) -> None:
        outer = ttk.Frame(self.root, padding=12)
        outer.pack(fill="both", expand=True)

        summary = ttk.LabelFrame(outer, text="运行概况", padding=10)
        summary.pack(fill="x")
        fields = (
            ("当前模式", self.mode_var),
            ("当前状态", self.state_var),
            ("今日发送", self.today_var),
            ("每日上限", self.limit_var),
            ("成功/匹配", self.success_var),
            ("跳过", self.skipped_var),
            ("失败", self.failed_var),
        )
        for index, (label, variable) in enumerate(fields):
            column = index % 4
            row = (index // 4) * 2
            ttk.Label(summary, text=label).grid(row=row, column=column, padx=(0, 22), sticky="w")
            value = ttk.Label(summary, textvariable=variable)
            value.grid(row=row + 1, column=column, padx=(0, 22), pady=(0, 7), sticky="w")
        if self.config is not None and not self.config.dry_run:
            ttk.Label(summary, text="警告：当前配置允许真实发送", foreground="#b00020").grid(
                row=4, column=0, columnspan=4, sticky="w"
            )

        message_frame = ttk.LabelFrame(outer, text="当前邀约话术（运行中只读，修改请编辑 config.json）", padding=8)
        message_frame.pack(fill="x", pady=(10, 0))
        self.message_text = tk.Text(message_frame, height=3, wrap="word")
        self.message_text.pack(fill="x")
        self.message_text.insert("1.0", self.config.message if self.config else "")
        self.message_text.configure(state="disabled")

        current = ttk.LabelFrame(outer, text="当前候选人", padding=8)
        current.pack(fill="x", pady=(10, 0))
        ttk.Label(current, textvariable=self.candidate_var).pack(anchor="w")
        ttk.Label(current, textvariable=self.result_var, foreground="#555555").pack(anchor="w", pady=(4, 0))

        controls = ttk.Frame(outer)
        controls.pack(fill="x", pady=10)
        self.action_buttons: list[ttk.Button] = []
        actions = (
            ("连接 BOSS 客户端", self._connect_client),
            ("页面诊断", self._diagnose),
            ("开始/继续", self._resume),
            ("暂停", self._pause),
            ("停止", self._stop),
        )
        for text, command in actions:
            button = ttk.Button(controls, text=text, command=command)
            button.pack(side="left", padx=(0, 7))
            self.action_buttons.append(button)
        ttk.Button(controls, text="打开日志目录", command=lambda: self._open_directory(self.log_dir)).pack(
            side="right", padx=(7, 0)
        )
        ttk.Button(controls, text="打开截图目录", command=lambda: self._open_directory(self.screenshot_dir)).pack(
            side="right"
        )

        log_frame = ttk.LabelFrame(outer, text="运行日志", padding=6)
        log_frame.pack(fill="both", expand=True)
        scrollbar = ttk.Scrollbar(log_frame)
        scrollbar.pack(side="right", fill="y")
        self.log_text = tk.Text(log_frame, wrap="word", state="disabled", yscrollcommand=scrollbar.set)
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
            self.controller.connect_client()

    def _diagnose(self) -> None:
        if self.controller:
            self.controller.diagnose()

    def _resume(self) -> None:
        if not self.controller or not self.config:
            return
        if not self.config.dry_run and not self._formal_confirmed:
            confirmed = messagebox.askyesno(
                "确认正式发送",
                "config.json 中 dry_run 已设置为 false。继续后可能真实发送邀约消息。\n\n"
                f"今日上限：{self.config.daily_limit}\n话术：{self.config.message}\n\n确认继续吗？",
                parent=self.root,
            )
            if not confirmed:
                return
            self._formal_confirmed = True
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
                    name = str(event.get("name", ""))
                    summary = str(event.get("summary", ""))
                    self.candidate_var.set(name)
                    self.result_var.set(summary)
                elif event_type == "candidate_result":
                    self.result_var.set(str(event.get("result", "")))
                elif event_type == "counts":
                    self.today_var.set(str(event.get("today_sent", 0)))
                    self.success_var.set(str(event.get("success", 0)))
                    self.skipped_var.set(str(event.get("skipped", 0)))
                    self.failed_var.set(str(event.get("failed", 0)))
                elif event_type == "diagnosis":
                    result = event.get("result", {})
                    if isinstance(result, dict):
                        self.state_var.set(f"诊断完成：识别 {result.get('recognized_candidate_count', 0)} 张候选人卡片")
                elif event_type == "worker_stopped":
                    if self.state_var.get() == "正在停止":
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
