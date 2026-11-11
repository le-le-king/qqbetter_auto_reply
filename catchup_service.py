"""断线补消息。

## 为什么需要这个

OneBot 的 WS 是**纯推送**，没有重放。插件和 LLBot 之间一断，那段时间的事件
直接消失——LLBot 不会为"断开的客户端"缓存任何东西。

第二种更隐蔽的丢法：LLBot 和 QQ 服务器断了（网络抖动、QQ 掉线、机器休眠），
QQ 会把离线期间的消息补推给 LLBot，但 LLBot 的 `reportOfflineMessage` 默认
是 **false**，不往回吐。插件这边看着一切正常，就是没消息。

两种丢法的共同点是：**消息其实还躺在 QQ 的历史里**，只是没人去拿。所以解法
不是让 LLBot 记得更多，而是自己回去对账。

## 怎么做

1. 每条消息入队前记下它的 `message_seq`（会话内单调递增），落盘到
   `data/message_cursor.json`。
2. WS 一建立（无论正向拨通还是反向被拨），就去拉每个已知会话的最近 N 条历史。
3. 把 `seq` 比游标大的、对方发的、时间还在窗口内的，按顺序注回正常管道。

历史接口返回的就是标准 OneBot 事件格式（连 `post_type` 都带），所以注入走的
是同一条 `_process_incoming`，引用展开、合并转发、语音转写一个都不会漏。

## 为什么不用"纯拉取"代替推送

用户直觉是"让她自己去查，别等推送"。但 OneBot 没有"有新消息吗"的长轮询
接口，只能定时翻历史——翻勤了是持续打 QQ 接口，翻疏了延迟就到分钟级。
所以推送仍是主路径（延迟最低），拉取只做**对账和补偿**。这两者是互补的：
推送负责"及时"，拉取负责"不漏"。
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Optional

from utils.file_utils import atomic_write_json_async, read_json_async


class QQMessageCatchupService:
    """重连后把断线期间漏掉的 QQ 消息补回正常管道。"""

    CURSOR_FILENAME = "message_cursor.json"
    SCHEMA_VERSION = 1

    #: 一次最多补几个会话。多到没边的话，一次重连要打十几个 QQ 接口，
    #: 既慢又容易被风控，而且补回来的东西她也看不过来。
    MAX_CONVERSATIONS = 8
    #: 每个会话最多翻几页。一页 40 条，三页够覆盖一整夜。
    MAX_PAGES = 3
    #: 每次拉取条数（QQ 端上限附近，实测 60 条能一次拿到 15 小时）
    FETCH_COUNT = 40
    #: 记住最近处理过的 message_id，用来兜住"游标慢一拍"。
    #: 2026-11-01 实际事故：插件重载时游标还没落盘（有 3 秒防抖），重连后
    #: 上一轮刚回过的消息被当成漏消息又补了一遍，她对着同一条链接回了两次。
    #: 游标是"位置"，这个是"名单"，名单比位置可靠。
    MAX_RECENT_IDS = 400

    #: 两次补消息之间的最小间隔，防止连接反复抖动时把接口打爆
    MIN_INTERVAL_SECONDS = 30.0
    #: ``call_action`` 对不存在的接口会抛 RuntimeError，文案各实现不一样，
    #: 只能靠这几个词认。"1404" 是 NapCat 系对未知 action 的 retcode。
    UNSUPPORTED_MARKS = (
        "1404", "不支持", "not supported", "unknown action",
        "no such action", "未实现", "undefine", "not found",
    )
    #: 补回来的消息之间的注入间隔：让下游缓冲有机会把它们合并成一轮
    DEFAULT_STAGGER_SECONDS = 0.12

    def __init__(self, plugin: Any):
        self.plugin = plugin
        self.logger = getattr(plugin, "logger", None)

        #: key = "private:<qq>" / "group:<群号>"，value = {seq, at, kind, peer, updated}
        self._cursors: dict[str, dict[str, Any]] = {}
        #: message_id → 处理时间。只留最近 MAX_RECENT_IDS 条。
        self._recent_ids: "OrderedDict[str, float]" = OrderedDict()
        self._loaded = False
        self._dirty = False
        self._lock = asyncio.Lock()
        self._flush_task: Optional[asyncio.Task] = None
        self._sweep_task: Optional[asyncio.Task] = None
        self._last_catchup_at: float = 0.0
        self._last_result: dict[str, Any] = {}
        #: 分项计数。吞异常必须留数（2026-10-31 的教训）：接口不支持、真没消息、
        #: 调用失败三种情况处置完全不同，混成一个"补了 0 条"等于没观测。
        self._diag: dict[str, int] = {
            "catchup_runs": 0,
            "conv_ok": 0,
            "conv_empty": 0,
            "conv_unsupported": 0,
            "conv_failed": 0,
            "injected": 0,
            "skipped_capped": 0,
            "skipped_old": 0,
            "skipped_self": 0,
            "skipped_dup": 0,
            "seq_missing": 0,
        }
        self._consecutive_failures = 0

    # ── 配置 ────────────────────────────────────────────────

    def _cfg(self, key: str, default: Any) -> Any:
        settings = getattr(self.plugin, "_qq_settings", None) or {}
        value = settings.get(key, default)
        return default if value is None else value

    def _enabled(self) -> bool:
        return bool(self._cfg("catchup_enabled", True))

    # ── 持久化 ──────────────────────────────────────────────

    def _path(self) -> Path:
        try:
            base = Path(self.plugin.data_path())
        except Exception:
            base = Path(".")
        return base / self.CURSOR_FILENAME

    async def start(self) -> None:
        """加载游标 + 起落盘循环 + 起定期对账。挂在 startup，跟 QQ 连接无关。"""
        await self._load()
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.create_task(self._flush_loop())
        if self._sweep_task is None or self._sweep_task.done():
            self._sweep_task = asyncio.create_task(self._sweep_loop())

    async def stop(self) -> None:
        for attr in ("_flush_task", "_sweep_task"):
            task = getattr(self, attr, None)
            setattr(self, attr, None)
            if task is not None and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        await self._flush(force=True)

    async def _sweep_loop(self) -> None:
        """定期对账。

        为什么不能只在重连时补：**LLBot 和 QQ 之间断开、但插件和 LLBot 之间
        没断**的情况，我们这边收不到任何"重连"信号——LLBot 不会因为自己掉了
        QQ 就重拨插件。那段时间的事件照样丢，而且永远不会自动补。

        所以每隔几分钟主动翻一次历史，跟游标对一下。游标是最新的时候这一趟
        什么都不会做（一次接口调用，几十字节），有缺口就当场补上。
        """
        while True:
            try:
                await asyncio.sleep(30.0)  # 启动后先缓一会儿，别和连接时的补消息撞车
                while True:
                    minutes = float(self._cfg("catchup_sweep_minutes", 5) or 0)
                    if minutes <= 0:
                        break
                    await asyncio.sleep(minutes * 60)
                    client = getattr(self.plugin, "qq_client", None)
                    if client is None or not self._enabled():
                        continue
                    if not getattr(self.plugin, "_running", False):
                        continue
                    try:
                        if not client.is_connected():
                            continue
                    except Exception:
                        pass
                    await self.catch_up(reason="sweep")
            except asyncio.CancelledError:
                break
            except Exception:
                if self.logger:
                    self.logger.warning("[Catchup] 定期对账异常", exc_info=True)
                await asyncio.sleep(60.0)

    async def _load(self) -> None:
        if self._loaded:
            return
        path = self._path()
        payload: Any = None
        if path.is_file():
            try:
                payload = await read_json_async(path)
            except Exception:
                # 游标坏了不该影响启动——最坏结果就是这次少补一些，下次照常
                if self.logger:
                    self.logger.warning("[Catchup] 游标文件读取失败，按空处理", exc_info=True)
                payload = None
        conversations = (payload or {}).get("conversations") if isinstance(payload, dict) else None
        if isinstance(conversations, dict):
            for key, rec in conversations.items():
                if not isinstance(rec, dict):
                    continue
                try:
                    seq = int(rec.get("seq") or 0)
                except (TypeError, ValueError):
                    seq = 0
                self._cursors[str(key)] = {
                    "seq": seq,
                    "at": int(rec.get("at") or 0),
                    "kind": str(rec.get("kind") or ""),
                    "peer": str(rec.get("peer") or ""),
                    "updated": int(rec.get("updated") or 0),
                }
        ids = (payload or {}).get("recent_ids") if isinstance(payload, dict) else None
        if isinstance(ids, dict):
            for mid, ts in ids.items():
                try:
                    self._recent_ids[str(mid)] = float(ts or 0)
                except (TypeError, ValueError):
                    continue
        self._loaded = True

    async def _flush(self, *, force: bool = False) -> None:
        if not self._dirty and not force:
            return
        payload = {
            "version": self.SCHEMA_VERSION,
            "saved_at": int(time.time()),
            "conversations": self._cursors,
            "recent_ids": dict(self._recent_ids),
        }
        try:
            await atomic_write_json_async(self._path(), payload)
            self._dirty = False
        except Exception:
            if self.logger:
                self.logger.warning("[Catchup] 游标落盘失败", exc_info=True)

    async def _flush_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(1.0)
                await self._flush()
            except asyncio.CancelledError:
                break
            except Exception:
                if self.logger:
                    self.logger.warning("[Catchup] 落盘循环异常", exc_info=True)

    # ── 钩子 ────────────────────────────────────────────────

    @staticmethod
    def _conversation_key(message: dict[str, Any]) -> str:
        if str(message.get("message_type")) == "group":
            gid = str(message.get("group_id") or "").strip()
            return f"group:{gid}" if gid else ""
        uid = str(message.get("user_id") or "").strip()
        if not uid:
            # 私聊事件的 user_id 就是对方；自己发的事件（message_sent）也带
            # user_id，但下面 note_message 会把它们过滤掉
            uid = str((message.get("sender") or {}).get("user_id") or "").strip()
        return f"private:{uid}" if uid else ""

    def note_message(self, message: dict[str, Any]) -> None:
        """记录"处理到哪了"。同步、只动内存，落盘交给后台循环。

        由 ``QQClient._fire_message`` 在收流循环里直接调用，所以这里绝对不能
        await——一 await 整条 WS 就卡住了。
        """
        try:
            if str(message.get("post_type")) != "message":
                return
            key = self._conversation_key(message)
            if not key:
                return
            # 先记名单再算位置：就算这条算不出 seq，"这条已经处理过"这个事实
            # 也必须留下——名单是防重复注入的最后一道闸。
            self._remember_id(message)

            rec = self._cursors.get(key)
            if rec is None:
                kind, peer = key.split(":", 1)
                rec = {"seq": 0, "at": 0, "kind": kind, "peer": peer, "updated": 0}
                self._cursors[key] = rec
            rec["updated"] = int(time.time())
            ts = int(message.get("time") or 0)
            if ts:
                rec["at"] = ts

            try:
                seq = int(message.get("message_seq") or 0)
            except (TypeError, ValueError):
                seq = 0
            if seq > 0:
                # 只在**没被标脏**的时候用 seq 推进。标脏说明之前混进过
                # 不可比的数，得等下次对账重新对齐，否则会跳到一个错误的位置。
                if not rec.get("seq_unknown") and seq > int(rec.get("seq") or 0):
                    rec["seq"] = seq
            else:
                # 拿不到 message_seq 时**绝对不能拿时间戳凑**。
                # 2026-11-01 踩过：拿 time*1000（一万亿量级）当 seq 写进去，
                # 之后所有历史消息的 seq（一百多）都"比它小"，补消息就永久失效；
                # 反过来如果写得比真实值小，重连后又会把回过的消息再补一遍。
                # 正确做法是标记"位置不明"，让下次对账只对齐、不注入。
                rec["seq_unknown"] = True
                self._diag["seq_missing"] += 1
            self._dirty = True
        except Exception:
            if self.logger:
                self.logger.warning("[Catchup] 记录游标失败", exc_info=True)

    def _remember_id(self, message: dict[str, Any]) -> None:
        """记住处理过的 message_id（去重名单）。"""
        mid = str(message.get("message_id") or "").strip()
        if not mid:
            return
        self._recent_ids[mid] = time.time()
        self._recent_ids.move_to_end(mid)
        while len(self._recent_ids) > self.MAX_RECENT_IDS:
            self._recent_ids.popitem(last=False)

    async def on_connected(self, reason: str) -> None:
        """连接建立后的回调。故意不抛异常：补消息失败不该影响收消息。"""
        try:
            await self.catch_up(reason=reason)
        except Exception:
            if self.logger:
                self.logger.warning("[Catchup] 补消息失败", exc_info=True)

    # ── 主流程 ──────────────────────────────────────────────

    def _self_id(self) -> str:
        client = getattr(self.plugin, "qq_client", None)
        return str(getattr(client, "_self_id", "") or "").strip()

    def _conversations_to_check(self, limit: Optional[int] = None) -> list[str]:
        """要补哪些会话：按最近活跃排序，外加管理员私聊兜底。"""
        admin = str(getattr(self.plugin, "_admin_qq", "") or "").strip()
        cap = self.MAX_CONVERSATIONS if limit is None else max(1, int(limit))
        ordered = sorted(
            self._cursors.items(),
            key=lambda kv: int(kv[1].get("updated") or 0),
            reverse=True,
        )
        picked = [key for key, _ in ordered[:cap]]
        # 管理员那条永远要补，哪怕它排不进最近的几个：主人发的话漏了最难受
        if admin:
            admin_key = f"private:{admin}"
            if admin_key not in picked:
                picked.insert(0, admin_key)
        return picked

    async def catch_up(self, *, reason: str = "manual", force: bool = False) -> dict[str, Any]:
        """重连后对账补消息。返回这次做了什么，供日志和状态查询用。"""
        client = getattr(self.plugin, "qq_client", None)
        if client is None or not self._enabled():
            return {"status": "disabled"}
        if not self._loaded:
            await self._load()

        now = time.time()
        if not force and now - self._last_catchup_at < self.MIN_INTERVAL_SECONDS:
            return {"status": "throttled", "since": round(now - self._last_catchup_at, 1)}

        async with self._lock:
            self._last_catchup_at = now
            self._diag["catchup_runs"] += 1
            window = max(1.0, float(self._cfg("catchup_window_hours", 12) or 12)) * 3600
            per_conv = max(1, int(self._cfg("catchup_max_per_conversation", 12) or 12))
            total_cap = max(1, int(self._cfg("catchup_max_total", 30) or 30))
            stagger = max(
                0.0, float(self._cfg("catchup_stagger_ms", 120) or 120) / 1000.0
            )

            result: dict[str, Any] = {
                "status": "ok",
                "reason": reason,
                "conversations": {},
                "injected": 0,
                "skipped": 0,
            }
            budget = total_cap
            # 定期对账只扫最近的 3 个会话、预算也收窄：它是兜底通道，不该像
            # 重连补偿那样一次性扫全。每 5 分钟 3 次接口调用，对 QQ 侧很轻。
            sweep = reason == "sweep"
            if sweep:
                budget = min(budget, 6)

            for key in self._conversations_to_check(limit=3 if sweep else None):
                if budget <= 0:
                    break
                rec = self._cursors.get(key)
                if rec is None:
                    # 必须登记进字典再往下传：_catch_up_conversation 是就地改
                    # rec 的，传一个游离的 dict 进去，游标就写不回来了。
                    nk, np_ = key.split(":", 1)
                    rec = {"seq": 0, "at": 0, "kind": nk, "peer": np_, "updated": 0}
                    self._cursors[key] = rec
                try:
                    detail = await self._catch_up_conversation(
                        key, rec, window=window, limit=min(per_conv, budget),
                        stagger=stagger, force=force,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._diag["conv_failed"] += 1
                    self._note_failure(key)
                    if self.logger:
                        self.logger.warning(f"[Catchup] 会话 {key} 补消息异常", exc_info=True)
                    continue
                result["conversations"][key] = detail
                result["injected"] += int(detail.get("injected") or 0)
                result["skipped"] += int(detail.get("skipped") or 0)
                budget -= int(detail.get("injected") or 0)

            self._dirty = True
            await self._flush()
            self._last_result = result
            self._log_result(result)
            return result

    async def _catch_up_conversation(
        self,
        key: str,
        rec: dict[str, Any],
        *,
        window: float,
        limit: int,
        stagger: float,
        force: bool = False,
    ) -> dict[str, Any]:
        kind, peer = key.split(":", 1)
        cursor_seq = int(rec.get("seq") or 0)
        self_id = self._self_id()
        now = time.time()

        # 从没见过的会话（首次启动、新加的好友）：**只对齐游标，不补消息**。
        # 不这么做的话，第一次跑起来会把服务端历史里整个窗口的消息全倒进管道，
        # 她会对着几小时前的话一口气回十二条，看起来像抽风。
        #
        # ``seq_unknown`` 同样走这条路：那说明现场事件里取不到 message_seq，
        # 现在这个游标是不可信的，只能拿历史重新对齐一次，不能拿它去判定
        # "哪些是漏的"——判错的代价是把回过的消息再回一遍。
        #
        # 手动触发（reason="manual"）时跳过这道闸，方便验证和救急。
        align_only = (
            (not int(rec.get("updated") or 0) and cursor_seq <= 0)
            or bool(rec.get("seq_unknown"))
        )

        collected: list[dict[str, Any]] = []
        anchor = 0
        pages = 0
        truncated = False
        while pages < self.MAX_PAGES:
            fetched, status = await self._fetch_history(kind, peer, anchor)
            pages += 1
            if status != "ok":
                return {"status": status, "injected": 0, "skipped": 0, "pages": pages}
            if not fetched:
                break
            collected.extend(fetched)
            seqs = [self._seq_of(m) for m in fetched]
            oldest = min(seqs) if seqs else 0
            times = [int(m.get("time") or 0) for m in fetched]
            times = [t for t in times if t]
            oldest_time = min(times) if times else 0
            if align_only and not force:
                break
            if oldest <= cursor_seq + 1:
                break
            if oldest_time and now - oldest_time > window:
                # 已经翻到窗口之外，再往前都是"陈年老账"，没必要补
                break
            anchor = oldest
        else:
            truncated = True

        if not collected:
            self._diag["conv_empty"] += 1
            return {"status": "empty", "injected": 0, "skipped": 0, "pages": pages}

        if align_only and not force:
            max_seq = max((self._seq_of(m) for m in collected), default=0)
            times = [int(m.get("time") or 0) for m in collected]
            times = [t for t in times if t]
            rec.update({
                "seq": max_seq,
                "kind": kind,
                "peer": peer,
                "updated": int(now),
                "at": max(times) if times else 0,
            })
            # 对齐完成，"位置不明"的标记可以摘了
            rec.pop("seq_unknown", None)
            if self_id:
                rec["self_id"] = self_id
            return {"status": "realigned", "injected": 0, "skipped": 0, "pages": pages}

        missed: list[dict[str, Any]] = []
        skipped_old = 0
        skipped_self = 0
        skipped_dup = 0
        seen_ids: set[str] = set()
        for msg in collected:
            if str(msg.get("post_type")) != "message":
                # message_sent = 她自己发的，补回来会导致自我回答
                continue
            seq = self._seq_of(msg)
            if seq <= cursor_seq:
                continue
            mid = str(msg.get("message_id") or "")
            if mid and mid in seen_ids:
                continue
            if mid:
                seen_ids.add(mid)
            if mid and mid in self._recent_ids:
                # 这条其实处理过（游标没来得及落盘时会出现），不再补一遍
                skipped_dup += 1
                continue
            sender = str((msg.get("sender") or {}).get("user_id") or msg.get("user_id") or "")
            if self_id and sender == self_id:
                skipped_self += 1
                continue
            ts = int(msg.get("time") or 0)
            if ts and now - ts > window:
                skipped_old += 1
                continue
            missed.append(msg)

        self._diag["skipped_self"] += skipped_self
        self._diag["skipped_old"] += skipped_old
        self._diag["skipped_dup"] += skipped_dup

        # 时间排序（seq 在跨实现时不一定严格单调，时间更可靠）
        missed.sort(key=lambda m: (int(m.get("time") or 0), self._seq_of(m)))
        skipped_capped = 0
        if len(missed) > limit:
            skipped_capped = len(missed) - limit
            # 留最新的：几小时前的第一条和最后一条比，最后一条才是他真正在等回应
            missed = missed[-limit:]
            self._diag["skipped_capped"] += skipped_capped

        injected = 0
        for msg in missed:
            if not getattr(self.plugin, "_running", False):
                break
            ts = int(msg.get("time") or 0)
            msg["_catchup"] = True
            msg["_catchup_reason"] = "catchup"
            msg["_catchup_age_seconds"] = int(now - ts) if ts else 0
            try:
                await self.plugin.qq_client.inject_message(msg)
                injected += 1
                self._diag["injected"] += 1
            except Exception:
                self._note_failure(key)
                if self.logger:
                    self.logger.warning(f"[Catchup] 注入 {key} 消息失败", exc_info=True)
                break
            if stagger:
                await asyncio.sleep(stagger)

        # 游标推到"见过的最大 seq"，包括没注入的（她自己发的、太老的、被限流的）。
        # 不推的话下次重连还会把它们翻出来，白白多打一轮接口。
        max_seq = max((self._seq_of(m) for m in collected), default=0)
        if max_seq > cursor_seq:
            rec["seq"] = max_seq
            rec["kind"] = kind
            rec["peer"] = peer
            rec["updated"] = int(now)
            latest_ts = max((int(m.get("time") or 0) for m in collected), default=0)
            if latest_ts:
                rec["at"] = latest_ts
        if self_id:
            rec["self_id"] = self_id

        self._diag["conv_ok"] += 1
        self._consecutive_failures = 0
        return {
            "status": "ok",
            "injected": injected,
            "skipped": skipped_capped + skipped_old + skipped_self + skipped_dup,
            "skipped_capped": skipped_capped,
            "skipped_old": skipped_old,
            "skipped_self": skipped_self,
            "skipped_dup": skipped_dup,
            "pages": pages,
            "truncated": truncated,
            "cursor": rec.get("seq"),
        }

    @staticmethod
    def _seq_of(msg: dict[str, Any]) -> int:
        try:
            seq = int(msg.get("message_seq") or 0)
        except (TypeError, ValueError):
            seq = 0
        if seq > 0:
            return seq
        try:
            return int(float(msg.get("time") or 0)) * 1000
        except (TypeError, ValueError):
            return 0

    async def _fetch_history(
        self, kind: str, peer: str, anchor: int,
    ) -> tuple[list[dict[str, Any]], str]:
        """拉一页历史。返回 (消息列表, 状态)。

        ⚠️ 注意返回值形状：``QQClient.call_action`` 已经把 OneBot 信封里的
        ``data`` **拆出来**了，所以这里拿到的是 ``{"messages": [...]}``，
        不是 ``{"status","retcode","data"}``。按信封解析会永远得到空列表
        （2026-11-01 实测踩过：日志显示"补充 0 条, status=empty"，看着像
        "断线期间真没消息"，其实是解析错位）。

        状态分三种，因为处置完全不一样：
          - ``ok``：拿到了（列表可能是空的，那是真没消息）
          - ``unsupported``：这个 OneBot 实现没有该接口——``call_action`` 对
            ``status=failed`` 会抛 ``RuntimeError``，只能从文案里认。
          - ``failed``：超时/连接断了/返回形状不认识
        """
        client = getattr(self.plugin, "qq_client", None)
        if client is None:
            return [], "failed"
        try:
            if kind == "private":
                data = await client.get_friend_msg_history(
                    peer, message_seq=int(anchor), count=self.FETCH_COUNT
                )
            else:
                data = await client.get_group_msg_history(
                    peer, message_seq=int(anchor), count=self.FETCH_COUNT
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            text = str(exc)
            if any(mark in text for mark in self.UNSUPPORTED_MARKS):
                self._diag["conv_unsupported"] += 1
                if self.logger:
                    self.logger.info(f"[Catchup] {kind}:{peer} 不支持历史接口: {text[:80]}")
                return [], "unsupported"
            self._diag["conv_failed"] += 1
            if self.logger:
                self.logger.warning(f"[Catchup] 拉取 {kind}:{peer} 历史失败: {text[:120]}")
            return [], "failed"

        if isinstance(data, list):
            messages = data
        elif isinstance(data, dict):
            # 常见的三种字段名：messages（LLBot/NapCat）、message_list、纯列表兜底
            messages = data.get("messages")
            if messages is None:
                messages = data.get("message_list")
            if messages is None and not data:
                # 空 dict = call_action 拿到的 data 是空的，可能是超时后被
                # `or {}` 吞掉。这不能算"真没消息"，必须报失败。
                self._diag["conv_failed"] += 1
                return [], "failed"
        else:
            self._diag["conv_failed"] += 1
            return [], "failed"

        if not isinstance(messages, list):
            self._diag["conv_failed"] += 1
            return [], "failed"
        return [m for m in messages if isinstance(m, dict)], "ok"

    def _note_failure(self, key: str) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures == 3 and self.logger:
            # 连续失败 3 次升到 warning 以上：偶发一次可能是网络，连着三次
            # 就是接口改了或者参数名变了，不能继续安静地"补了 0 条"。
            self.logger.error(
                f"[Catchup] 连续 {self._consecutive_failures} 次补消息失败（最近会话 {key}），"
                "断线补偿可能已经失效"
            )

    def _log_result(self, result: dict[str, Any]) -> None:
        if not self.logger:
            return
        injected = int(result.get("injected") or 0)
        convs = result.get("conversations") or {}
        detail = ", ".join(
            f"{k}={v.get('injected', 0)}/{v.get('status')}" for k, v in convs.items()
        )
        if injected:
            self.logger.info(
                f"[Catchup] 补回 {injected} 条漏消息（{result.get('reason')}）: {detail}"
            )
        else:
            # 一条没补是常态（断线期间本来就没消息），降到 debug 免得日志被刷
            self.logger.debug(f"[Catchup] 无需补消息（{result.get('reason')}）: {detail}")

    # ── 状态 ────────────────────────────────────────────────

    def describe_state(self) -> dict[str, Any]:
        now = time.time()
        return {
            "enabled": self._enabled(),
            "conversations": len(self._cursors),
            "recent_ids": len(self._recent_ids),
            "last_catchup_seconds_ago": (
                round(now - self._last_catchup_at, 1) if self._last_catchup_at else None
            ),
            "last_result": self._last_result,
            "diag": dict(self._diag),
            "cursor_preview": {
                key: {
                    "seq": rec.get("seq"),
                    "age_minutes": (
                        round((now - int(rec.get("at") or 0)) / 60, 1)
                        if rec.get("at") else None
                    ),
                }
                for key, rec in sorted(
                    self._cursors.items(),
                    key=lambda kv: int(kv[1].get("updated") or 0),
                    reverse=True,
                )[:8]
            },
        }
