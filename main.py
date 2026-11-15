"""到点提醒 / 到点回访。

她答应「半小时后提醒老大喝水」「中午来找你」「晚上 8 点叫你」之后，真的会在那个时间
自己发一条消息过来——不是闹钟文案，是让主模型按人格现写一句。

实现方式：不自己起定时器，直接挂到 AstrBot 的 cron（active_agent 任务）上。好处是
  - 重启不丢（写进 data_v4.db 的 cron_jobs）；
  - 跑的时候由平台把主 agent 唤醒，人格、上下文、工具都是现成的；
  - 在 Dashboard 的「定时任务」里能看见、能删。

所以这个插件只干两件事：
  1. 给模型一个说人话的入口（remind_me 工具），把「几点 + 提醒啥」翻译成 cron 任务；
  2. 把唤醒时用的 note 写好，避免她到点念一句「系统提醒您……」。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

try:  # 用户侧注入用（本轮附加内容，不动系统提示词）
    from astrbot.core.agent.message import TextPart  # type: ignore
except Exception:  # noqa: BLE001
    try:
        from astrbot.api.message_components import TextPart  # type: ignore
    except Exception:  # noqa: BLE001
        TextPart = None  # type: ignore


PLUGIN_NAME = "astrbot_plugin_reminder"
PLUGIN_VERSION = "0.1.1"

DEFAULTS = {
    "default_minutes": 30,      # 没给具体时间（「等我吃完饭再聊」）时默认多少分钟后回来
    "max_horizon_days": 30,     # 最多排到多少天以后
    "min_lead_seconds": 60,     # 最短提前量：算出来太近就压到 60 秒后
}

KINDS = ("remind", "come_back")

REPLY_TEMPLATE = {
    "remind": (
        "⏰ 到点了。老大之前让你在 {when} 提醒他：{what}。\n"
        "现在用你自己的语气跟他说这件事——就像平时发消息一样，一两句话就够，"
        "别像闹钟，也别提「定时任务」「提醒系统」「系统提示」这类词。"
    ),
    "come_back": (
        "⏰ 到点了。老大之前说：{what}。\n"
        "现在按约定回来找他，用你自己的语气自然接一句（一两句话），"
        "别像闹钟，也别提定时任务或系统提示。"
    ),
}


def _fmt(target: datetime) -> str:
    return target.strftime("%m-%d %H:%M")


@register(PLUGIN_NAME, "paopao", "到点提醒/到点回访：让她在约定时间主动来找你", PLUGIN_VERSION)
class ReminderPlugin(Star):
    def __init__(self, context: Context, config: dict[str, Any] | None = None):
        super().__init__(context)
        self.context = context
        cfg = config if isinstance(config, dict) else {}
        self.default_minutes = max(1, int(cfg.get("default_minutes", DEFAULTS["default_minutes"]) or 30))
        self.max_horizon_days = max(1, int(cfg.get("max_horizon_days", DEFAULTS["max_horizon_days"]) or 30))
        self.min_lead_seconds = max(10, int(cfg.get("min_lead_seconds", DEFAULTS["min_lead_seconds"]) or 60))

    # ── 时区 ────────────────────────────────────────────────────
    def _tz(self, event: AstrMessageEvent) -> tuple[Any, str]:
        name = ""
        try:
            name = str(self.context.get_config(umo=event.unified_msg_origin).get("timezone") or "")
        except Exception:  # noqa: BLE001
            name = ""
        if name:
            try:
                return ZoneInfo(name), name
            except ZoneInfoNotFoundError:
                logger.warning("[Reminder] 时区 %s 不认识，退回系统时区", name)
        return datetime.now().astimezone().tzinfo, "local"

    # ── 工具 ────────────────────────────────────────────────────
    @filter.llm_tool(name="remind_me")
    async def remind_me(
        self,
        event: AstrMessageEvent,
        action: str = "create",
        what: str = "",
        minutes: float = 0,
        at: str = "",
        kind: str = "remind",
        job_id: str = "",
    ) -> str:
        """给老大排一个「到点提醒 / 到点回来找他」的定时任务。到点你会被叫醒，自己发消息过去。

        什么时候用（必须真的调用，不许只用嘴说「好的我记下了」）：
        - 「半小时后提醒我喝水」「20 分钟后叫我」「一小时后来找我」→ 用 minutes
        - 「晚上 8 点来找我」「中午来找我」「明天早上 7 点提醒我带伞」→ 自己换算成具体时间填 at
          （at 用 ISO 格式，带时区，例如 2026-11-14T12:00:00+08:00）
        - 「等我吃完饭再聊」「我先去忙，一会儿回来」这类没给时间的 → minutes 留空，默认 30 分钟后回来找他

        **改时间也算一次调用（最容易漏的一条）**：
        - 老大说「一小时太长了，半小时」「改成明天早上」→ 用 action=reschedule，
          它会自动把同一个事件的旧提醒取消掉再按新时间排，不需要你去找 job_id。
        - 只要老大提到了时间 + 提醒/叫我/来找我，就必须调用一次工具；
          只回一句「行，那就半小时」= 什么也没排上，到点不会响。

        - 想看看已经排了哪些 → action=list；想取消 → action=cancel 并带上 job_id

        Args:
            action(string): create（排新的）/ reschedule（改时间：先取消旧的再排新的）/ list / cancel
            what(string): 要提醒的事或要说的主题，用老大原话里的关键词，例如「喝水」「带伞」「回来找我聊」
            minutes(number): 多少分钟之后；给了 at 时以 at 为准
            at(string): 具体时间，ISO 格式带时区，例如 2026-11-14T20:00:00+08:00
            kind(string): remind=提醒一件事；come_back=到点自己回来找他说话
            job_id(string): cancel 时要取消的任务 id（list 会返回）

        Returns:
            str: 排好之后会告诉你排在哪一刻，你再用一句自然的话跟老大确认。
        """
        try:
            return await self._handle(event, action=action, what=what, minutes=minutes,
                                      at=at, kind=kind, job_id=job_id)
        except Exception as error:  # noqa: BLE001
            logger.warning("[Reminder] 排提醒失败 error_type=%s", type(error).__name__, exc_info=True)
            return f"排提醒的时候出错了（{type(error).__name__}），跟老大说一声这次没定上。"

    async def _handle(self, event: AstrMessageEvent, *, action, what, minutes, at, kind, job_id) -> str:
        manager = getattr(self.context, "cron_manager", None)
        if manager is None:
            return "定时任务管理器不可用，这次没法排。"
        umo = str(event.unified_msg_origin or "")
        action = (action or "create").strip().lower()

        if action == "list":
            return await self._list(manager, umo)
        if action == "cancel":
            return await self._cancel(manager, umo, job_id)
        if action == "reschedule":
            await self._cancel_same(manager, umo, what)
        return await self._create(manager, event, umo, what=what, minutes=minutes, at=at, kind=kind)

    async def _cancel_same(self, manager, umo, what: str) -> None:
        """改时间时先把这个会话里同一件事的旧提醒撤掉，免得到点两次都响。"""
        try:
            jobs = await manager.list_jobs("active_agent")
        except Exception:  # noqa: BLE001
            return
        key = (what or "").strip()
        for job in jobs:
            payload = getattr(job, "payload", None)
            if not isinstance(payload, dict) or str(payload.get("session") or "") != umo:
                continue
            old = str(payload.get("what") or "")
            if key and old and key not in old and old not in key:
                continue
            try:
                await manager.delete_job(job.job_id)
                logger.info("[Reminder] 改时间，取消旧任务 job=%s what=%s", job.job_id, old)
            except Exception:  # noqa: BLE001
                continue

    async def _create(self, manager, event, umo, *, what, minutes, at, kind) -> str:
        tz, tz_name = self._tz(event)
        now = datetime.now(tz)
        kind = (kind or "remind").strip().lower()
        if kind not in KINDS:
            kind = "remind"
        what = (what or "").strip()
        if not what:
            what = "回来找他聊" if kind == "come_back" else "提醒他"

        target = None
        if (at or "").strip():
            target = self._parse_at(at.strip(), tz)
            if target is None:
                return "at 的时间格式看不懂，换成 ISO 格式（2026-11-14T20:00:00+08:00）再排一次。"
            # 过了点的时间当“明天那个点”
            if target <= now and (now - target) < timedelta(days=1):
                target = target + timedelta(days=1)
        elif minutes and float(minutes) > 0:
            target = now + timedelta(minutes=float(minutes))
        else:
            target = now + timedelta(minutes=self.default_minutes)

        earliest = now + timedelta(seconds=self.min_lead_seconds)
        if target < earliest:
            target = earliest
        if target - now > timedelta(days=self.max_horizon_days):
            return f"排得太远了（超过 {self.max_horizon_days} 天），这次没排。"

        note = REPLY_TEMPLATE[kind].format(when=_fmt(target), what=what)
        job = await manager.add_active_job(
            name=f"reminder-{what[:16]}",
            cron_expression=None,
            payload={
                "session": umo,
                "sender_id": str(event.get_sender_id() or ""),
                "note": note,
                "origin": "tool",
                "run_at": target.isoformat(),
                "kind": kind,
                "what": what,
            },
            description=note,
            timezone=tz_name if tz_name != "local" else None,
            enabled=True,
            persistent=True,
            run_once=True,
            run_at=target,
        )
        delta_min = int((target - now).total_seconds() // 60)
        logger.info("[Reminder] 已排：%s（%s 分钟后）what=%s job=%s",
                    target.isoformat(), delta_min, what, getattr(job, "job_id", ""))
        return (
            f"已排好：{_fmt(target)}（约 {delta_min} 分钟后）去{'提醒他' if kind == 'remind' else '找他'}"
            f"「{what}」。任务 id：{getattr(job, 'job_id', '')}。"
            f"跟老大确认的时候用一句自然的话，别报参数。"
        )

    async def _list(self, manager, umo) -> str:
        try:
            jobs = await manager.list_jobs("active_agent")
        except Exception as error:  # noqa: BLE001
            logger.warning("[Reminder] 读任务列表失败 error_type=%s", type(error).__name__)
            return "读不到定时任务列表。"
        mine = [job for job in jobs
                if isinstance(getattr(job, "payload", None), dict)
                and str(job.payload.get("session") or "") == umo]
        if not mine:
            return "这个会话目前没有排着的提醒。"
        lines = []
        for job in mine[:10]:
            payload = job.payload or {}
            when = ""
            run_at = payload.get("run_at")
            if run_at:
                when = str(run_at)[:16].replace("T", " ")
            lines.append(f"- {when} {payload.get('what') or job.name}（id {job.job_id}）")
        return "排着的提醒：\n" + "\n".join(lines)

    async def _cancel(self, manager, umo, job_id) -> str:
        job_id = (job_id or "").strip()
        if not job_id:
            return "要取消哪个？把 list 里的 id 给我。"
        try:
            jobs = await manager.list_jobs("active_agent")
        except Exception:  # noqa: BLE001
            jobs = []
        match = None
        for job in jobs:
            payload = getattr(job, "payload", None)
            if not isinstance(payload, dict) or str(payload.get("session") or "") != umo:
                continue
            if str(job.job_id) == job_id or str(job.job_id).startswith(job_id):
                match = job
                break
        if match is None:
            return f"没找到 id 是 {job_id} 的提醒（可能已经响过了）。"
        await manager.delete_job(match.job_id)
        return f"取消了：{_fmt_from_payload(match.payload)} {match.payload.get('what') or ''}".strip()

    # ── 兜底一：给模型现挂一条硬规则 ────────────────────────────
    REMINDER_HINT = (
        "\n\n【提醒功能硬规则】老大这句话提到了时间 + 提醒/叫我/喊我/找我，"
        "你必须调用一次 remind_me 工具。只回一句「行」「好」等于什么都没排，到点不会响。"
        "改时间（「半小时太长了，改成一小时」）用 action=reschedule，它会先撤旧的再排新的。"
        "排完再用一句自然的话确认，别报参数。"
    )
    INTENT_WORDS = ("提醒", "叫我", "喊我", "闹钟", "来找我", "回来找我", "来喊我")
    TIME_WORDS = ("分钟", "小时", "点半", "点", "明天", "今晚", "中午", "早上", "晚上", "下午",
                  "一会儿", "等下", "待会", "半小时", "一小时后")

    @filter.on_llm_request()
    async def inject_reminder_rule(self, event: AstrMessageEvent, req: Any):
        """只在「像是要设提醒」的那一轮加一句规则，平时不动系统提示词（缓存友好）。"""
        text = str(getattr(event, "message_str", "") or "")
        if not text:
            return
        if not any(w in text for w in self.INTENT_WORDS):
            return
        if not any(w in text for w in self.TIME_WORDS):
            return
        hint = self.REMINDER_HINT.strip()
        # 首选：作为「本轮用户侧附加内容」注入。
        # 好处是系统提示词一个字都不动，前缀缓存保持有效；
        # 这份内容只作用于当前这一轮，不会写进历史。
        try:
            parts = getattr(req, "extra_user_content_parts", None)
            if parts is not None and TextPart is not None:
                for part in parts:
                    if "提醒功能硬规则" in str(getattr(part, "text", "")):
                        return
                part = TextPart(text=hint)
                make_temp = getattr(part, "make_as_temp", None)
                if callable(make_temp):  # 新版本支持标记为临时内容
                    part = make_temp()
                parts.append(part)
                return
        except Exception:  # noqa: BLE001
            logger.debug("[Reminder] 用户侧注入失败，回退系统提示词", exc_info=True)

        # 回退：老版本没有 extra_user_content_parts，才动系统提示词
        try:
            current = str(getattr(req, "system_prompt", "") or "")
            if "提醒功能硬规则" in current:
                return
            req.system_prompt = current + self.REMINDER_HINT
        except Exception:  # noqa: BLE001
            logger.debug("[Reminder] 注入硬规则失败", exc_info=True)

    # ── 兜底二：/提醒 命令，不靠模型也能排 ──────────────────────
    @filter.command("提醒")
    async def remind_cmd(self, event: AstrMessageEvent):
        """指令：/提醒 5分钟 喝水 ｜ /提醒 半小时 喝水 ｜ /提醒列表 ｜ /提醒取消 <id>"""
        manager = getattr(self.context, "cron_manager", None)
        if manager is None:
            yield event.plain_result("定时任务管理器不可用，这次没法排。")
            return
        umo = str(event.unified_msg_origin or "")
        text = str(getattr(event, "message_str", "") or "").strip()
        for prefix in ("/提醒", "提醒"):
            if text.startswith(prefix):
                text = text[len(prefix):].strip()
                break
        if text.startswith("列表") or text.lower().startswith("list"):
            yield event.plain_result(await self._list(manager, umo))
            return
        if text.startswith("取消") or text.lower().startswith("cancel"):
            rest = text[2:].strip() if text.startswith("取消") else text[6:].strip()
            yield event.plain_result(await self._cancel(manager, umo, rest))
            return

        minutes, what = self._split_delay(text)
        if minutes is None:
            minutes = self.default_minutes
        if not what:
            what = "回来找他聊"
        yield event.plain_result(
            await self._create(manager, event, umo, what=what, minutes=minutes, at="", kind="remind")
        )

    def _split_delay(self, text: str) -> tuple[float | None, str]:
        """「5分钟 喝水」→ (5, '喝水')；「半小时」→ (30, '')。"""
        import re

        text = text.strip()
        m = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*(分钟|分|小时|时|个小时|h|min)?\s*(.*)$", text)
        if m and m.group(1):
            value = float(m.group(1))
            unit = (m.group(2) or "分钟").lower()
            if unit in ("小时", "时", "个小时", "h"):
                value *= 60
            return value, (m.group(3) or "").strip()
        if "一个半小时" in text:
            return 90, text.replace("一个半小时", "").strip()
        if "半小时" in text:
            return 30, text.replace("半小时", "").strip()
        if "一小时" in text:
            return 60, text.replace("一小时", "").strip()
        return None, text

    @staticmethod
    def _parse_at(text: str, tz) -> datetime | None:
        candidate = text.strip().replace(" ", "T", 1) if " " in text and "T" not in text else text.strip()
        for value in (candidate, text.strip()):
            try:
                parsed = datetime.fromisoformat(value)
            except ValueError:
                continue
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=tz)
            return parsed
        return None


def _fmt_from_payload(payload) -> str:
    run_at = (payload or {}).get("run_at")
    if not run_at:
        return ""
    return str(run_at)[:16].replace("T", " ")
