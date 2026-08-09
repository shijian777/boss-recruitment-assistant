from __future__ import annotations

import json
import queue
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from app.candidate_filter import evaluate_candidate
from app.config import AppConfig
from app.conversation import ChatMessage, ConversationSnapshot, InboxItemSnapshot
from app.database import CandidateRecord, Database, ReplyRecord
from app.desktop import CandidateSnapshot, DesktopBossClient
from app.reply_policy import ReplyPolicyDecision, evaluate_reply_policy
from app.reply_rules import choose_reply
from app.safety import RunControl, detect_risk
from app.selectors import normalize_space


@dataclass(slots=True)
class RuntimeCounters:
    success: int = 0
    skipped: int = 0
    failed: int = 0
    consecutive_failures: int = 0
    batch_sent: int = 0
    session_contacted: int = 0


class AutomationController:
    """UI 只投递命令；所有 Windows UI Automation 对象均留在工作线程。"""

    def __init__(
        self,
        config: AppConfig,
        database: Database,
        base_dir: Path,
        screenshot_dir: Path,
        event_queue: "queue.Queue[dict[str, object]]",
        logger: Any,
    ) -> None:
        self.config = config
        self.database = database
        self.base_dir = base_dir
        self.screenshot_dir = screenshot_dir
        self.event_queue = event_queue
        self.logger = logger
        self._commands: "queue.Queue[str]" = queue.Queue()
        self._reply_messages: "queue.Queue[str]" = queue.Queue()
        self._thread: threading.Thread | None = None
        self._thread_lock = threading.RLock()
        self._control = RunControl(initially_paused=True)
        self._last_unread_messages: int | None = None

    @property
    def is_alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _emit(self, event_type: str, **payload: object) -> None:
        self.event_queue.put({"type": event_type, **payload})

    def _ensure_worker(self) -> None:
        with self._thread_lock:
            if self.is_alive:
                return
            self._control = RunControl(initially_paused=True)
            self._commands = queue.Queue()
            self._reply_messages = queue.Queue()
            self._thread = threading.Thread(
                target=self._worker_main,
                name="boss-desktop-uia-worker",
                daemon=True,
            )
            self._thread.start()

    def connect_client(self) -> None:
        self._ensure_worker()
        self._commands.put("open")

    def diagnose(self) -> None:
        self._ensure_worker()
        self._commands.put("diagnose")

    def refresh_stats(self) -> None:
        self._ensure_worker()
        self._commands.put("refresh_stats")

    def reply_current_chat(self, message: str) -> None:
        self._ensure_worker()
        self._reply_messages.put(message)
        self._commands.put("reply_current_chat")

    def watch_current_chat(self) -> None:
        self._ensure_worker()
        self._commands.put("watch_current_chat")

    def watch_inbox(self) -> None:
        """Start an explicitly enabled loop over semantic unread inbox entries."""
        self._ensure_worker()
        self._commands.put("watch_inbox")

    def update_config(self, config: AppConfig) -> None:
        with self._thread_lock:
            self.config = config
        if self.is_alive:
            self._commands.put("update_config")
        self.logger.info(
            "面板设置已更新：本次邀约 %s 人，间隔 %s 秒，每日上限 %s 人",
            config.session_limit,
            config.interval_seconds,
            config.daily_limit,
        )

    def resume(self) -> None:
        self._ensure_worker()
        self._control.resume()
        self._commands.put("run")

    def pause(self) -> None:
        self._control.pause()
        self.logger.info("用户请求暂停")
        self._emit("state", state="已暂停")

    def stop(self) -> None:
        self.logger.info("用户请求停止")
        self._control.stop()
        self._commands.put("stop")
        self._emit("state", state="正在停止")

    def shutdown(self, timeout: float = 5.0) -> bool:
        self.stop()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self.database.close()
        return thread is None or not thread.is_alive()

    def _record(self, snapshot: CandidateSnapshot, status: str, *, error: str = "") -> None:
        self.database.record(
            CandidateRecord(
                candidate_key=snapshot.key,
                candidate_name=snapshot.name,
                candidate_url=snapshot.url,
                candidate_summary=snapshot.summary,
                status=status,
                message="BOSS 客户端岗位默认招呼" if status == "dry_run_match" else "",
                error_message=error,
            )
        )

    def _finalize(
        self,
        snapshot: CandidateSnapshot,
        status: str,
        *,
        error: str = "",
        message: str = "",
    ) -> None:
        self.database.finalize(
            CandidateRecord(
                candidate_key=snapshot.key,
                candidate_name=snapshot.name,
                candidate_url=snapshot.url,
                candidate_summary=snapshot.summary,
                status=status,
                message=message if status == "sent" else "",
                error_message=error,
            )
        )

    def _emit_counts(
        self,
        counters: RuntimeCounters,
        client: DesktopBossClient | None = None,
    ) -> None:
        unread_messages = self._last_unread_messages
        if client is not None and client.is_open:
            try:
                unread_messages = client.unread_message_count()
                self._last_unread_messages = unread_messages
            except Exception as exc:
                self.logger.warning("读取 BOSS 未读消息数失败：%s", exc)
        stats = self.database.dashboard_stats()
        self._emit(
            "counts",
            today_contacted=stats.contacted,
            today_sent=stats.sent,
            today_unverified=stats.unverified,
            today_failed=stats.failed,
            today_replied=stats.replied,
            unread_messages=unread_messages,
            success=counters.success,
            skipped=counters.skipped,
            failed=counters.failed,
            session_contacted=counters.session_contacted,
        )

    def _fatal_risk(self, client: DesktopBossClient, keyword: str) -> None:
        screenshot = client.save_screenshot("risk_stop")
        self.logger.error("检测到风险关键词“%s”，已紧急停止。截图：%s", keyword, screenshot)
        self._emit("state", state=f"风险停止：{keyword}")
        self._control.stop()

    def _page_risk(self, client: DesktopBossClient) -> str | None:
        try:
            return detect_risk(client.body_text(), self.config.risk_keywords)
        except Exception as exc:
            self.logger.warning("读取页面风险文本失败：%s", exc)
            return None

    def _reply_policy_decision(self, conversation_key: str) -> ReplyPolicyDecision:
        now = datetime.now().astimezone()
        last_sent_at = self.database.last_reply_sent_at()
        if last_sent_at is not None and last_sent_at.tzinfo is None:
            last_sent_at = last_sent_at.replace(tzinfo=now.tzinfo)
        return evaluate_reply_policy(
            today_sent=self.database.today_reply_sent_count(),
            conversation_sent=self.database.conversation_reply_sent_count(
                conversation_key
            ),
            last_sent_at=last_sent_at,
            now=now,
            daily_limit=self.config.auto_reply.daily_limit,
            per_conversation_limit=self.config.auto_reply.per_conversation_limit,
            interval_seconds=self.config.auto_reply.interval_seconds,
        )

    def _process_candidate(
        self,
        client: DesktopBossClient,
        snapshot: CandidateSnapshot,
        counters: RuntimeCounters,
        session_seen: set[str],
    ) -> None:
        session_seen.add(snapshot.key)
        self._emit("candidate", name=snapshot.name, summary=snapshot.summary[:240])

        if counters.session_contacted >= self.config.session_limit:
            self.logger.info("本次邀约人数已达到上限 %s，停止运行", self.config.session_limit)
            self._emit("state", state="已达到本次邀约人数")
            self._control.stop()
            return

        if self.database.already_handled(snapshot.key, dry_run=self.config.dry_run):
            self._record(snapshot, "duplicate")
            counters.skipped += 1
            self.logger.info("跳过已处理候选人：%s", snapshot.name)
            self._emit("candidate_result", result="已跳过：数据库中已有处理记录")
            self._emit_counts(counters, client)
            return

        decision = evaluate_candidate(snapshot.summary, self.config.candidate_filter)
        if not decision.matched:
            self._record(snapshot, "filtered", error=decision.reason)
            counters.skipped += 1
            self.logger.info("规则跳过 %s：%s", snapshot.name, decision.reason)
            self._emit("candidate_result", result=f"已跳过：{decision.reason}")
            self._emit_counts(counters, client)
            return

        if self.config.dry_run:
            self._record(snapshot, "dry_run_match")
            counters.success += 1
            counters.session_contacted += 1
            counters.consecutive_failures = 0
            self.logger.info("测试模式匹配：%s（绝不点击沟通或发送）", snapshot.name)
            self._emit("candidate_result", result="测试模式匹配：未点击、未发送")
            self._emit_counts(counters, client)
            return

        if self.database.today_contacted_count() >= self.config.daily_limit:
            self.logger.info("今日发送数已达到上限 %s，停止运行", self.config.daily_limit)
            self._emit("state", state="已达到每日上限")
            self._control.stop()
            return

        risk = self._page_risk(client)
        if risk:
            self._fatal_risk(client, risk)
            return

        if not self.database.reserve(snapshot.key):
            self._record(snapshot, "duplicate")
            counters.skipped += 1
            self.logger.info("候选人正在由另一发送流程处理，已跳过：%s", snapshot.name)
            self._emit_counts(counters, client)
            return

        try:
            result = client.send_greeting(snapshot)
            if result.success:
                self._finalize(snapshot, "sent", message=result.message)
                counters.success += 1
                counters.batch_sent += 1
                counters.session_contacted += 1
                counters.consecutive_failures = 0
                self.logger.info("已验证发送成功：%s", snapshot.name)
                self._emit("candidate_result", result="发送成功并已验证")
                self._emit_counts(counters, client)
                if not self._control.interruptible_sleep(self.config.interval_seconds):
                    return
                if counters.batch_sent >= self.config.batch_size:
                    counters.batch_sent = 0
                    self.logger.info("批次完成，暂停 %s 秒", self.config.batch_pause_seconds)
                    self._emit("state", state=f"批次暂停 {self.config.batch_pause_seconds} 秒")
                    self._control.interruptible_sleep(self.config.batch_pause_seconds)
                    if not self._control.is_stopped:
                        self._emit("state", state="运行中")
                return

            failure_status = (
                "failed"
                if result.platform_blocked
                else ("greeting_unverified" if result.attempted else "failed")
            )
            self._finalize(snapshot, failure_status, error=result.error)
            counters.failed += 1
            counters.consecutive_failures += 1
            if result.attempted and not result.platform_blocked:
                counters.session_contacted += 1
            try:
                screenshot = client.save_screenshot(failure_status)
                self.logger.error("发送失败或无法验证：%s；截图：%s", result.error, screenshot)
            except Exception as screenshot_exc:
                self.logger.error(
                    "发送失败或无法验证：%s；同时保存截图失败：%s",
                    result.error,
                    screenshot_exc,
                )
            self._emit("candidate_result", result=f"失败：{result.error}")
            self._emit_counts(counters, client)
            if result.platform_blocked:
                self.logger.error("自动点击已完成，但平台明确未发送；已暂停，且不计入成功发送或今日沟通")
                self._control.pause()
                self._emit("state", state="平台权益不足，消息未发送")
                return
            if result.attempted:
                self.logger.error("打招呼动作可能已经生效，已禁止自动重试并暂停等待人工核对")
                self._control.pause()
                self._emit("state", state="招呼结果未知，已防重并暂停人工核对")
                return
            if counters.consecutive_failures >= self.config.max_consecutive_failures:
                self.logger.error("连续失败达到上限，停止运行")
                self._emit("state", state="连续失败达到上限，已停止")
                self._control.stop()
            else:
                self._control.pause()
                self._emit("state", state="发送未验证，等待人工检查")
        except Exception as exc:
            self.database.release(snapshot.key)
            raise RuntimeError(f"发送流程异常：{exc}") from exc

    def _handle_current_reply(
        self,
        client: DesktopBossClient,
        message: str,
        counters: RuntimeCounters,
    ) -> None:
        if not client.is_open:
            self.logger.warning("请先连接 BOSS 客户端并手动打开一个聊天会话")
            self._emit("state", state="请先连接并打开聊天会话")
            return
        risk = self._page_risk(client)
        if risk:
            self._fatal_risk(client, risk)
            return

        result = client.send_current_chat_message(message, dry_run=self.config.dry_run)
        if result.success:
            if self.config.dry_run:
                self.database.record_reply(ReplyRecord("reply_dry_run", message))
                self.logger.info("测试模式：当前会话回复控件预检通过，未填入、未点击、未发送")
                self._emit("state", state="回复预检通过（测试模式未发送）")
            else:
                self.database.record_reply(ReplyRecord("reply_sent", message))
                self.logger.info("当前会话回复已发送并验证成功")
                self._emit("state", state="当前会话回复发送成功")
            self._emit_counts(counters, client)
            return

        status = "reply_unverified" if result.attempted else "reply_failed"
        self.database.record_reply(ReplyRecord(status, message, result.error))
        try:
            screenshot = client.save_screenshot(status)
            self.logger.error("当前会话回复失败：%s；截图：%s", result.error, screenshot)
        except Exception as screenshot_exc:
            self.logger.error("当前会话回复失败：%s；保存截图失败：%s", result.error, screenshot_exc)
        self._emit_counts(counters, client)
        self._control.pause()
        if result.attempted:
            self._emit("state", state="回复结果未知，已暂停人工核对")
        else:
            self._emit("state", state="未识别到唯一聊天输入区，已暂停")

    def _handle_auto_reply(
        self,
        client: DesktopBossClient,
        conversation: ConversationSnapshot,
        incoming: ChatMessage,
        counters: RuntimeCounters,
    ) -> bool:
        if self.database.has_replied_to(incoming.key):
            self.logger.info("新来信已存在回复记录，跳过重复处理")
            return True
        decision = choose_reply(
            incoming.text,
            self.config.auto_reply.rules,
            self.config.auto_reply.stop_keywords,
        )
        common = {
            "conversation_key": conversation.key,
            "incoming_key": incoming.key,
            "rule_name": decision.rule_name,
        }
        if decision.action != "reply":
            self.database.record_reply(
                ReplyRecord(
                    "reply_skipped",
                    "",
                    decision.reason,
                    **common,
                )
            )
            self.logger.info("新来信未自动回复：%s", decision.reason)
            self._emit("state", state=f"新来信未自动回复：{decision.reason}")
            return True

        if not self.config.dry_run:
            policy = self._reply_policy_decision(conversation.key)
            if policy.action == "pause":
                self.logger.info("%s，已暂停", policy.reason)
                self._control.pause()
                self._emit("state", state=policy.reason)
                return False
            if policy.action == "manual":
                self.database.record_reply(
                    ReplyRecord("reply_skipped", "", policy.reason, **common)
                )
                self.logger.info("新来信转人工：%s", policy.reason)
                self._emit("state", state=f"新来信转人工：{policy.reason}")
                return True
            if policy.action == "wait":
                self._emit(
                    "state",
                    state=f"自动回复等待 {policy.wait_seconds} 秒",
                )
                return False

        # Re-read immediately before sending. A manual conversation switch
        # between detection and send must never target a different person.
        latest = client.read_current_conversation()
        if latest.key != conversation.key or all(item.key != incoming.key for item in latest.messages):
            self.logger.warning("发送前会话或来信已变化，已取消自动回复")
            self._control.pause()
            self._emit("state", state="会话已变化，自动回复已暂停")
            return False

        result = client.send_current_chat_message(
            decision.reply,
            dry_run=self.config.dry_run,
        )
        if result.success:
            if self.config.dry_run:
                self.database.record_reply(
                    ReplyRecord("reply_dry_run", decision.reply, **common)
                )
                self.logger.info("测试模式命中“%s”：未填写、未点击、未发送", decision.rule_name)
                self._emit("state", state=f"测试命中话术：{decision.rule_name}（未发送）")
            else:
                self.database.record_reply(
                    ReplyRecord("reply_sent", decision.reply, **common)
                )
                self.logger.info("自动回复已验证成功；使用话术“%s”", decision.rule_name)
                self._emit("state", state=f"自动回复成功：{decision.rule_name}")
            self._emit_counts(counters, client)
            return True

        status = "reply_unverified" if result.attempted else "reply_failed"
        self.database.record_reply(
            ReplyRecord(status, decision.reply, result.error, **common)
        )
        try:
            screenshot = client.save_screenshot(status)
            self.logger.error("自动回复失败：%s；截图：%s", result.error, screenshot)
        except Exception as screenshot_exc:
            self.logger.error("自动回复失败：%s；保存截图失败：%s", result.error, screenshot_exc)
        self._control.pause()
        self._emit_counts(counters, client)
        self._emit("state", state="自动回复失败或结果未知，已暂停")
        return True

    def _handle_inbox_reply(
        self,
        client: DesktopBossClient,
        inbox_item: InboxItemSnapshot,
        counters: RuntimeCounters,
        inbox_seen: set[str],
    ) -> None:
        """Handle one unread list preview without trusting candidate-authored metadata."""
        if self.database.has_replied_to(inbox_item.incoming_key):
            inbox_seen.add(inbox_item.incoming_key)
            self.logger.info("该未读来信已存在发送记录，跳过重复处理")
            return

        decision = choose_reply(
            inbox_item.preview,
            self.config.auto_reply.rules,
            self.config.auto_reply.stop_keywords,
        )
        common = {
            "conversation_key": inbox_item.conversation_key,
            "incoming_key": inbox_item.incoming_key,
            "rule_name": decision.rule_name,
        }
        if decision.action != "reply":
            self.database.record_reply(
                ReplyRecord("reply_skipped", "", decision.reason, **common)
            )
            inbox_seen.add(inbox_item.incoming_key)
            self.logger.info("未读来信未自动回复：%s", decision.reason)
            self._emit("state", state=f"未读来信转人工：{decision.reason}")
            return

        if self.config.dry_run:
            self.database.record_reply(
                ReplyRecord("reply_dry_run", decision.reply, **common)
            )
            inbox_seen.add(inbox_item.incoming_key)
            self.logger.info(
                "测试模式命中未读话术“%s”：未打开会话、未填写、未点击、未发送",
                decision.rule_name,
            )
            self._emit("state", state=f"测试命中话术：{decision.rule_name}（未发送）")
            self._emit_counts(counters, client)
            return

        policy = self._reply_policy_decision(inbox_item.conversation_key)
        if policy.action == "pause":
            self.logger.info("%s，已暂停", policy.reason)
            self._control.pause()
            self._emit("state", state=policy.reason)
            return
        if policy.action == "manual":
            self.database.record_reply(
                ReplyRecord("reply_skipped", "", policy.reason, **common)
            )
            inbox_seen.add(inbox_item.incoming_key)
            self.logger.info("未读来信转人工：%s", policy.reason)
            self._emit("state", state=f"未读来信转人工：{policy.reason}")
            return
        if policy.action == "wait":
            self._emit(
                "state",
                state=f"自动回复等待 {policy.wait_seconds} 秒",
            )
            return

        # Reacquire and invoke the exact live unread list item. Selection is
        # verified on the right-hand chat pane before any text is entered.
        if not client.open_inbox_conversation(inbox_item):
            error = "未能唯一打开并验证目标未读会话"
            self.database.record_reply(
                ReplyRecord("reply_failed", decision.reply, error, **common)
            )
            inbox_seen.add(inbox_item.incoming_key)
            self.logger.error(error)
            self._control.pause()
            self._emit("state", state="目标会话验证失败，已暂停")
            return

        # The list preview is used only as a conservative pre-filter. After
        # selecting the exact unread item, read the full chat and require a
        # direction-confirmed incoming message that corresponds to that
        # preview, then run stop words and the library again on the full text.
        try:
            conversation = client.read_current_conversation()
        except Exception as exc:
            error = f"目标会话已打开，但无法读取完整消息：{exc}"
            self.database.record_reply(
                ReplyRecord("reply_failed", decision.reply, error, **common)
            )
            inbox_seen.add(inbox_item.incoming_key)
            self.logger.error(error)
            self._control.pause()
            self._emit("state", state="完整来信读取失败，已暂停")
            return
        preview = normalize_space(inbox_item.preview).rstrip(".…")
        matching_incoming = [
            message
            for message in conversation.messages
            if message.direction == "incoming"
            and (
                preview in normalize_space(message.text)
                or normalize_space(message.text) in preview
            )
        ]
        if not preview or not matching_incoming:
            error = "完整聊天中未找到与未读预览对应的对方消息"
            self.database.record_reply(
                ReplyRecord("reply_failed", decision.reply, error, **common)
            )
            inbox_seen.add(inbox_item.incoming_key)
            self.logger.error(error)
            self._control.pause()
            self._emit("state", state="来信身份复核失败，已暂停")
            return
        full_incoming = matching_incoming[-1]
        decision = choose_reply(
            full_incoming.text,
            self.config.auto_reply.rules,
            self.config.auto_reply.stop_keywords,
        )
        common["rule_name"] = decision.rule_name
        if decision.action != "reply":
            self.database.record_reply(
                ReplyRecord("reply_skipped", "", decision.reason, **common)
            )
            inbox_seen.add(inbox_item.incoming_key)
            self.logger.info("完整来信复核后未自动回复：%s", decision.reason)
            self._emit("state", state=f"完整来信转人工：{decision.reason}")
            return

        risk = self._page_risk(client)
        if risk:
            inbox_seen.add(inbox_item.incoming_key)
            self._fatal_risk(client, risk)
            return

        result = client.send_current_chat_message(decision.reply, dry_run=False)
        inbox_seen.add(inbox_item.incoming_key)
        if result.success:
            self.database.record_reply(
                ReplyRecord("reply_sent", decision.reply, **common)
            )
            self.logger.info("未读会话自动回复已验证成功；使用话术“%s”", decision.rule_name)
            self._emit("state", state=f"自动回复成功：{decision.rule_name}")
            self._emit_counts(counters, client)
            return

        status = "reply_unverified" if result.attempted else "reply_failed"
        self.database.record_reply(
            ReplyRecord(status, decision.reply, result.error, **common)
        )
        try:
            screenshot = client.save_screenshot(status)
            self.logger.error("未读会话自动回复失败：%s；截图：%s", result.error, screenshot)
        except Exception as screenshot_exc:
            self.logger.error(
                "未读会话自动回复失败：%s；保存截图失败：%s",
                result.error,
                screenshot_exc,
            )
        self._control.pause()
        self._emit_counts(counters, client)
        self._emit("state", state="自动回复失败或结果未知，已暂停")

    def _handle_diagnosis(self, client: DesktopBossClient) -> None:
        if not client.is_open:
            self.logger.warning("请先连接 BOSS 客户端，再执行页面诊断")
            self._emit("state", state="请先连接 BOSS 客户端")
            return
        result = client.diagnose(self.screenshot_dir)
        self.logger.info("页面诊断结果：\n%s", json.dumps(result, ensure_ascii=False, indent=2))
        self._emit("diagnosis", result=result)

    def _worker_main(self) -> None:
        client = DesktopBossClient(self.config, self.screenshot_dir, self.logger)
        counters = RuntimeCounters()
        session_seen: set[str] = set()
        active = False
        mode = "idle"
        candidate_page_attempted = False
        watched_conversation_key = ""
        reply_baseline: set[str] = set()
        inbox_seen: set[str] = set()
        next_reply_poll = 0.0
        try:
            self.logger.info("自动化工作线程已启动；Windows UI Automation 将仅在此线程中使用")
            while not self._control.is_stopped:
                try:
                    command = self._commands.get(timeout=0.15)
                except queue.Empty:
                    command = ""

                if command == "stop":
                    break
                if command == "open":
                    client.open()
                    self._control.pause()
                    active = False
                    mode = "idle"
                    self.logger.info("已连接 BOSS 客户端。请手动登录并进入候选人页，再点击“开始/继续”")
                    self._emit("state", state="已连接客户端，等待人工登录和定位页面")
                    self._emit_counts(counters, client)
                    continue
                if command == "diagnose":
                    self._handle_diagnosis(client)
                    continue
                if command == "refresh_stats":
                    if client.is_open:
                        client.refresh()
                        self._emit_counts(counters, client)
                    else:
                        self._emit("state", state="请先连接 BOSS 客户端")
                    continue
                if command == "update_config":
                    client.config = self.config
                    self.logger.info("工作线程已应用最新面板配置")
                    continue
                if command == "reply_current_chat":
                    try:
                        reply_message = self._reply_messages.get_nowait()
                    except queue.Empty:
                        self.logger.error("回复命令缺少话术，已取消")
                        continue
                    active = False
                    mode = "idle"
                    self._control.pause()
                    self._handle_current_reply(client, reply_message, counters)
                    continue
                if command == "watch_current_chat":
                    if not client.is_open:
                        self._emit("state", state="请先连接并打开一个聊天会话")
                        continue
                    if not self.config.auto_reply.enabled:
                        self._emit("state", state="请先勾选允许自动回复并保存设置")
                        continue
                    if not self.config.auto_reply.rules:
                        self._emit("state", state="话术库为空，无法开启自动回复")
                        continue
                    try:
                        baseline = client.read_current_conversation()
                    except Exception as exc:
                        self.logger.warning("无法建立当前会话监听基线：%s", exc)
                        self._emit("state", state=f"无法读取当前会话：{exc}")
                        continue
                    watched_conversation_key = baseline.key
                    reply_baseline = {item.key for item in baseline.messages}
                    next_reply_poll = time.monotonic() + self.config.auto_reply.poll_seconds
                    mode = "reply_watch"
                    active = True
                    self._control.resume()
                    self.logger.info(
                        "已开启当前会话自动回复监听：%s；基线消息 %s 条，不会补发历史消息",
                        baseline.label,
                        len(baseline.messages),
                    )
                    self._emit("state", state=f"监听当前会话：{baseline.label}")
                    continue
                if command == "watch_inbox":
                    if not client.is_open:
                        self._emit("state", state="请先连接 BOSS 客户端")
                        continue
                    if not self.config.auto_reply.enabled:
                        self._emit("state", state="请先勾选允许自动回复并保存设置")
                        continue
                    if not self.config.auto_reply.rules:
                        self._emit("state", state="话术库为空，无法开启自动回复")
                        continue
                    try:
                        opened = client.open_messages_page()
                    except Exception as exc:
                        self.logger.warning("进入消息页失败：%s", exc)
                        opened = False
                    if not opened:
                        self._emit("state", state="无法自动进入 BOSS 消息页")
                        continue
                    inbox_seen.clear()
                    next_reply_poll = time.monotonic()
                    mode = "inbox_watch"
                    active = True
                    self._control.resume()
                    self.logger.info(
                        "已开启全消息页未读自动回复；每 %s 秒检查一次，今日上限 %s 条",
                        self.config.auto_reply.poll_seconds,
                        self.config.auto_reply.daily_limit,
                    )
                    self._emit("state", state="正在检测所有未读来信")
                    continue
                if command == "run":
                    if not client.is_open:
                        self.logger.warning("请先点击“连接 BOSS 客户端”")
                        self._emit("state", state="请先连接 BOSS 客户端")
                        active = False
                        self._control.pause()
                        continue
                    active = True
                    mode = "candidate"
                    self._control.resume()
                    self._emit("state", state="运行中")

                if not active:
                    continue
                if self._control.is_paused:
                    continue
                if not client.is_open:
                    self.logger.warning("BOSS 客户端窗口已关闭，安全停止")
                    self._emit("state", state="BOSS 客户端已关闭")
                    self._control.stop()
                    continue

                if mode == "reply_watch":
                    if time.monotonic() < next_reply_poll:
                        continue
                    next_reply_poll = time.monotonic() + self.config.auto_reply.poll_seconds
                    risk = self._page_risk(client)
                    if risk:
                        self._fatal_risk(client, risk)
                        continue
                    try:
                        conversation = client.read_current_conversation()
                    except Exception as exc:
                        self.logger.warning("读取当前会话失败，已暂停：%s", exc)
                        self._control.pause()
                        self._emit("state", state=f"读取当前会话失败：{exc}")
                        continue
                    if conversation.key != watched_conversation_key:
                        watched_conversation_key = conversation.key
                        reply_baseline = {item.key for item in conversation.messages}
                        self.logger.info("检测到人工切换会话，已重建基线，不回复现有历史消息")
                        self._emit("state", state=f"已切换并监听：{conversation.label}")
                        continue
                    new_messages = [
                        item for item in conversation.messages if item.key not in reply_baseline
                    ]
                    ambiguous = [item for item in new_messages if item.direction == "unknown"]
                    if ambiguous:
                        self.logger.warning("检测到方向不明的新消息，未自动回复并暂停")
                        self._control.pause()
                        self._emit("state", state="新消息方向不明，已暂停人工核对")
                        continue
                    incoming_messages = [
                        item for item in new_messages if item.direction == "incoming"
                    ]
                    if incoming_messages:
                        newest = incoming_messages[-1]
                        combined_text = "\n".join(item.text for item in incoming_messages)
                        incoming = ChatMessage(newest.key, "incoming", combined_text)
                        processed = self._handle_auto_reply(
                            client,
                            conversation,
                            incoming,
                            counters,
                        )
                        if processed:
                            reply_baseline = {
                                item.key for item in conversation.messages
                            }
                    else:
                        reply_baseline = {item.key for item in conversation.messages}
                    continue

                if mode == "inbox_watch":
                    if time.monotonic() < next_reply_poll:
                        continue
                    next_reply_poll = time.monotonic() + self.config.auto_reply.poll_seconds
                    risk = self._page_risk(client)
                    if risk:
                        self._fatal_risk(client, risk)
                        continue
                    try:
                        if not client.is_messages_page() and not client.open_messages_page():
                            raise RuntimeError("无法保持在消息页")
                        unread_count = client.unread_message_count()
                        unread_items = client.scan_unread_inbox()
                    except Exception as exc:
                        self.logger.warning("扫描未读会话失败，已暂停：%s", exc)
                        self._control.pause()
                        self._emit("state", state=f"扫描未读会话失败：{exc}")
                        continue
                    pending = [
                        item
                        for item in unread_items
                        if item.incoming_key not in inbox_seen
                        and not self.database.has_replied_to(item.incoming_key)
                    ]
                    if pending:
                        self._handle_inbox_reply(client, pending[0], counters, inbox_seen)
                        continue
                    if unread_count > 0 and not unread_items:
                        try:
                            screenshot = client.save_screenshot("unread_selector_unknown")
                            self.logger.warning(
                                "侧栏显示 %s 条未读，但未可靠识别会话列表；截图：%s",
                                unread_count,
                                screenshot,
                            )
                        except Exception as screenshot_exc:
                            self.logger.warning("未读结构未知且截图失败：%s", screenshot_exc)
                        self._control.pause()
                        self._emit("state", state="检测到未读但无法识别会话，已暂停")
                    else:
                        self._emit("state", state=f"未读检测中：当前 {len(unread_items)} 个会话")
                    continue

                if mode != "candidate":
                    continue

                client.refresh()
                if client.dismiss_benign_popups():
                    self.logger.info("已自动关闭遮挡候选人列表的信息提示")
                    self._emit("state", state="已关闭信息提示，继续运行")
                    continue
                self._emit_counts(counters, client)
                if counters.session_contacted >= self.config.session_limit:
                    self._emit("state", state="已达到本次邀约人数")
                    self._control.stop()
                    continue
                if self.database.today_contacted_count() >= self.config.daily_limit:
                    self._emit("state", state="已达到每日上限")
                    self._control.stop()
                    continue
                risk = self._page_risk(client)
                if risk:
                    self._fatal_risk(client, risk)
                    continue

                snapshots = client.scan_candidates()
                on_candidate_page = client.is_candidate_page()
                remaining = [item for item in snapshots if item.key not in session_seen]
                if remaining:
                    candidate_page_attempted = False
                    self._process_candidate(client, remaining[0], counters, session_seen)
                    continue
                if not on_candidate_page and not candidate_page_attempted:
                    candidate_page_attempted = True
                    if client.open_candidate_page():
                        self.logger.info("已自动进入推荐候选人页")
                        self._emit("state", state="已进入推荐页，正在扫描候选人")
                        continue
                    screenshot = client.save_screenshot("candidate_navigation_failed")
                    self.logger.warning("无法自动进入推荐页，已截图并暂停：%s", screenshot)
                    self._emit(
                        "state",
                        state="无法自动进入推荐页，请确认 BOSS 左侧存在“推荐”后重试",
                    )
                    self._control.pause()
                    active = False
                    continue
                if on_candidate_page and self.config.auto_scroll and client.scroll_candidates(session_seen):
                    self.logger.info("已自动滚动并加载新的候选人卡片")
                    self._emit("state", state="已自动加载更多候选人，继续运行")
                    continue
                if snapshots and client.click_next_page():
                    self.logger.info("页面变化后将重新获取候选人 UIA 控件")
                    continue

                screenshot = client.save_screenshot("selector_or_page_end")
                if not snapshots and not on_candidate_page:
                    self.logger.warning("未识别到候选人卡片，已截图并暂停：%s", screenshot)
                    self._emit("state", state="未识别到候选人卡片，已暂停")
                else:
                    self.logger.info("候选人列表已处理且自动滚动后没有新卡片，已暂停")
                    self._emit("state", state="当前候选人列表处理完毕")
                self._control.pause()
                active = False
        except Exception as exc:
            self.logger.exception("自动化线程异常：%s", exc)
            try:
                if client.is_open:
                    screenshot = client.save_screenshot("automation_exception")
                    self.logger.error("异常截图：%s", screenshot)
            except Exception:
                self.logger.exception("保存异常截图失败")
            self._emit("state", state=f"异常停止：{exc}")
        finally:
            try:
                client.close()
            except Exception:
                self.logger.exception("关闭浏览器失败")
            self.database.close()
            self._emit("worker_stopped")
            self.logger.info("自动化工作线程已安全退出")
