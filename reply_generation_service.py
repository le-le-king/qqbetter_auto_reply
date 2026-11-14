from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Optional

from main_logic.tool_calling import ToolResult
from utils.llm_client import AIMessage, SystemMessage, create_chat_llm_async
from utils.token_tracker import set_call_type

from .bilibili_browse_service import BILIBILI_TOOL_NAME
from .bilibili_video import BILIBILI_WATCH_TOOL_NAME, WATCH_TIMEOUT_SECONDS
from .gallery_service import AVATAR_TOOL_NAME, BROWSE_TOOL_NAME
from .memory_tool_service import RECALL_TOOL_HTTP_TIMEOUT_SECONDS
from .open_url_service import OPEN_TOOL_HTTP_TIMEOUT_SECONDS, OPEN_TOOL_NAME
from .pipeline_models import (
    QQModelResult,
    QQPipelineStageTrace,
    QQReplyContext,
    is_synthetic_source,
)
from .qzone_service import QZONE_TOOL_NAME
from .search_tool_service import (
    SEARCH_TOOL_HTTP_TIMEOUT_SECONDS,
    SEARCH_TOOL_NAME,
)
from .session_bootstrap_service import MAX_TOOL_ITERATIONS
from .sticker_service import SAVE_TOOL_NAME, SEND_TOOL_NAME

# 工具轮超时预算里，LLM 流那部分最多折算几段。
#
# 额度是 MAX_TOOL_ITERATIONS（5），理论上最坏 6 段流。但一段流正常只有
# 10~20 秒，6 段也就两分钟；真按 6×60 秒给预算，卡死的那一轮会把会话锁
# 占满七分钟（群聊里就是全群停摆），比直接判超时丢一轮更难收拾。
TOOL_TURN_STREAM_CAP = 4


class QQReplyGenerationService:
    def __init__(self, plugin: Any):
        self.plugin = plugin

    async def generate_reply_fallback_direct_llm(
        self,
        *,
        context: QQReplyContext,
    ) -> Optional[str]:
        try:
            from utils.config_manager import get_config_manager

            if self.plugin._should_skip_direct_llm_fallback_for_images(message=context.message, attachments=context.attachments):
                self.plugin.logger.warning("QQ 图片消息跳过纯文本 fallback，避免假装已看图")
                return None
            model_config = get_config_manager().get_model_api_config("conversation")
            base_url = str(model_config.get("base_url") or "").strip()
            model = str(model_config.get("model") or "").strip()
            api_key = str(model_config.get("api_key") or "").strip()
            if not base_url or not model:
                self.plugin.logger.warning("Fallback 生成跳过：agent 模型未配置")
                return None
            llm = await create_chat_llm_async(
                model=model,
                base_url=base_url,
                api_key=api_key,
                max_completion_tokens=120,
                timeout=float(self.plugin._ai_turn_timeout_seconds or 60.0) + 0.5,
                provider_type=model_config.get("provider_type"),
            )
            try:
                set_call_type("conversation")
                fb_prompt, fb_recalled = self._sanitize_for_live_consent(
                    context, context.system_prompt, context.recalled_memory_text,
                )
                # 与主会话路径对偶：清洗只保证"调用发起时"的授权，调用期间
                # 撤销的话返回文本里仍带着那些内容。
                consent_before = self._consent_dependency_snapshot(context)
                self._store_consent_snapshot(context, consent_before)
                response = await llm.ainvoke([
                    {"role": "system", "content": self._compose_turn_instructions(fb_prompt, fb_recalled)},
                    {"role": "user", "content": context.prompt_message},
                ])
                fallback_reply = getattr(response, "content", "") or ""
                if fallback_reply and self._consent_dependency_revoked(
                    context, consent_before,
                ):
                    self.plugin.logger.warning(
                        "生成期间记忆授权被撤销，丢弃 fallback 回复"
                    )
                    return None
                if fallback_reply:
                    self.plugin.logger.info(f"Fallback 直连 LLM 生成成功 (length: {len(fallback_reply)})")
                    return fallback_reply
                self.plugin.logger.warning("Fallback 直连 LLM 未生成内容")
                return None
            finally:
                aclose = getattr(llm, "aclose", None)
                if callable(aclose):
                    try:
                        await aclose()
                    except Exception:
                        pass
        except Exception as e:
            self.plugin.logger.warning(f"Fallback 直连 LLM 生成失败: {e}")
            return None

    async def generate_fallback_from_context(self, context: QQReplyContext) -> Optional[str]:
        return await self.generate_reply_fallback_direct_llm(context=context)

    async def run_primary_session_call(self, context: QQReplyContext) -> QQModelResult:
        session_key = self.plugin.session_runtime_service.build_generation_session_key(context)
        stage_trace = QQPipelineStageTrace(
            stage="model_primary",
            status="started",
            metadata={
                "session_key": session_key,
                "is_group": context.is_group,
                "group_id": str(context.group_id or ""),
                "ephemeral_session": context.ephemeral_session,
                "group_scene_mode": context.group_scene_mode,
            },
        )
        synthetic_hist_before = None
        try:
            user_data = await self.plugin.session_bootstrap_service.ensure_generation_session(context, session_key)
            if not user_data:
                stage_trace.status = "no_session"
                return QQModelResult(reply_text=None, source="none", traces=[stage_trace])

            # This evidence belongs to exactly one generation. Clear a
            # previous turn before prime/streaming can fail and leave a stale
            # truthy value for the delivery buffer to consume.
            user_data["human_row_materialized"] = False

            user_session, reply_chunks = self.plugin.session_runtime_service.prime_generation_session_state(
                user_data,
                session_key=session_key,
                context=context,
            )

            # 合成轮的 prompt 行在生成过程中进入历史：超时 salvage（下面
            # except 里的 discard→finalize）可能在 pipeline 的 finally 记录
            # 排除之前就把历史结算掉——生成前先记长度，超时时先排除再丢。
            synthetic_hist_before = (
                len(getattr(user_session, "_conversation_history", []) or [])
                if is_synthetic_source(getattr(context, "source_kind", ""))
                else None
            )

            try:
                ai_reply = await self._run_session_generation(
                    context=context,
                    session_key=session_key,
                    user_data=user_data,
                    user_session=user_session,
                    reply_chunks=reply_chunks,
                )
                pre_tool_text = str(
                    user_data.pop("current_pre_tool_text", "") or ""
                )
            finally:
                # 成员发言的收集绑定"会话已接受该 human 行"（stream_text 在
                # 发起网络流之前就把它追加进历史），不绑回复非空、也不绑
                # 生成成功：空回复轮与流异常/超时轮里成员的话都已进共享
                # 历史、会进群 digest，却会从 participant bucket 永久缺席。
                # 单点记录（成功钩子不再重复记）；recorder 自身按 sender
                # 追加，重复调用会重复入桶，故只此一处。
                if user_data.get("memory_enabled") and user_data.pop(
                    "human_row_accepted", False,
                ):
                    try:
                        self.plugin.session_memory_service.record_group_member_turn(
                            user_data, context,
                        )
                    except Exception as record_error:
                        # 绝不掩盖原始异常（finally 里抛出会替换掉
                        # TimeoutError，超时抢救与 trace 全部走偏）。
                        self.plugin.logger.warning(
                            f"成员发言记录失败: {record_error}"
                        )
            stage_trace.metadata["recalled_memory_used"] = context.recalled_memory_used
            stage_trace.metadata["recalled_memory_length"] = len(context.recalled_memory_text)
            if synthetic_hist_before is not None:
                # 合成控制轮（主动搭话等）的 human 行是捏造的指令，不是任何人
                # 真说过的话。写记忆之前先登记进排除名单——否则私聊 /cache 会
                # 把"[系统] 现在去主动找他说话……"连同六条视频候选一起当成
                # 主人说的话落库，下一轮她会自己接着念这段指令。
                # 超时分支另有一处调用（抢救要先排除再结算），两处都按对象
                # 身份去重，重复调用无副作用。
                try:
                    self.plugin.session_memory_service.record_synthetic_prompt_rows(
                        session_key, synthetic_hist_before,
                    )
                except Exception as exclude_error:
                    self.plugin.logger.warning(
                        f"合成轮 prompt 行排除失败（不影响回复）: {exclude_error}"
                    )
            if not ai_reply:
                self.plugin.logger.warning("AI 未生成回复，准备进入 fallback")
                stage_trace.status = "empty"
                stage_trace.metadata["reply_length"] = 0
                # 静默轮也要跑记忆管家：排空调度挂在这条路径上，一个模型
                # 一直选择沉默（或 fallback 也为空）的活跃群，否则群积压
                # 与成员队列永远不会被排空——队列到硬顶开始丢，历史被复读
                # 守卫重置时也没人抢救过。
                await self._run_memory_housekeeping(session_key, user_data)
                return QQModelResult(reply_text=None, source="session", allow_fallback=True, traces=[stage_trace])

            await self._sync_memory_after_success(
                session_key=session_key, user_data=user_data, context=context,
                reply_text=ai_reply,
            )
            self.plugin.logger.info(f"AI 生成回复完成 (会话: {session_key}, length: {len(ai_reply)})")
            stage_trace.status = "success"
            stage_trace.metadata["reply_length"] = len(ai_reply)
            return QQModelResult(
                reply_text=ai_reply,
                pre_tool_text=pre_tool_text,
                source="session",
                history_ai_row=user_data.get("current_turn_ai_row"),
                traces=[stage_trace],
            )

        except asyncio.TimeoutError:
            # discard_session 内部会先结算群 scoped 缓冲再丢弃（集中抢救）。
            self.plugin.logger.warning(f"会话 {session_key} 处理超时，关闭并丢弃该会话")
            if synthetic_hist_before is not None:
                # 抢救会立即 finalize：合成控制 prompt 行必须先进排除名单，
                # 否则 pipeline 层跑完后的记录来不及、控制指令被提取成
                # 参与者历史。
                try:
                    self.plugin.session_memory_service.record_synthetic_prompt_rows(
                        session_key, synthetic_hist_before,
                    )
                except Exception as salvage_error:
                    # 抢救标记失败不能连累丢弃：会话刚被强制取消，留着它
                    # 下一轮必再超时，且未结算状态会一直挂着。
                    self.plugin.logger.warning(
                        f"超时轮合成 prompt 行标记失败: {salvage_error}"
                    )
            discarded = await self.plugin.session_runtime_service.discard_session(session_key, reason="generation_timeout")
            if discarded is False:
                # 结算失败被有意保留：但本会话的 stream 刚被 wait_for 强制
                # 取消，直接复用会再次超时、陷入死循环。打粘性标记让下轮
                # bootstrap 先重试 discard（含集中抢救），与登录身份变化
                # 的 pending_identity_discard 模式对齐。
                kept = self.plugin._user_sessions.get(session_key)
                if kept is not None:
                    kept["pending_identity_discard"] = True
            stage_trace.status = "timeout"
            return QQModelResult(reply_text=None, source="session", timed_out=True, traces=[stage_trace])
        except Exception as e:
            self.plugin.logger.exception(f"AI 生成回复失败: {e}")
            stage_trace.status = "error"
            stage_trace.detail = str(e)
            return QQModelResult(reply_text=None, source="none", traces=[stage_trace])
        finally:
            if context.ephemeral_session:
                await self.plugin.session_runtime_service.discard_session(session_key, reason="ephemeral_cleanup")

    def _compose_turn_instructions(self, system_prompt: str, recalled_memory_text: str) -> str:
        return "\n\n".join(part for part in [system_prompt, recalled_memory_text] if part)

    async def _run_session_generation(
        self,
        *,
        context: QQReplyContext,
        session_key: str,
        user_data: dict[str, Any],
        user_session: Any,
        reply_chunks: list[str],
    ) -> str | None:
        async with user_data["lock"]:
            reply_chunks.clear()
            user_data["current_pre_tool_text"] = ""

            queued_images = await self.plugin._queue_attachment_images(user_session, context.attachments)
            self.plugin.logger.info(f"发送消息到 AI (会话: {session_key}, length: {len(context.prompt_message)}, images: {queued_images})")
            # 群会话是全群共享的：创建时烙进 system prompt 的是首个发言者的
            # member persona / 身份行。群轮必须无条件换上本轮刚构建好的
            # prompt（含当前发言人的 scoped persona），否则召回为空的轮次
            # （早期常态）会一直用创建者快照回答所有人。
            # 生成前最后一道复检（集中在一处，读点/构建后/锁内三段窗口
            # 共用同一判据）：共享会话锁与附件排队可能让本轮等很久，其间
            # 任一授权被撤销，已注入 prompt 的对应段都不得用于生成。
            turn_system_prompt, turn_recalled_text = self._sanitize_for_live_consent(
                context, context.system_prompt, context.recalled_memory_text,
            )
            # 生成前的依赖快照：模型已经读到 scoped/跨群内容后，撤销才落
            # 下的话，回复本身仍带着那些内容——生成结束要再比一次。
            consent_before = self._consent_dependency_snapshot(context)
            self._store_consent_snapshot(context, consent_before)
            history_before = len(
                getattr(user_session, "_conversation_history", []) or []
            )
            # 成员发言的收集绑定"共享历史真的收下了这条 human 行"。锁等待
            # 与附件排队都在 stream_text 之前，它们异常/取消/超时时这条消息
            # 根本没进历史——此时入 participant bucket 会造出会话里不存在的
            # 成员记忆。
            user_data["human_row_accepted"] = False
            user_data["human_row_materialized"] = False
            reply_attempt_state = user_data.setdefault(
                "reply_attempt_state", {"discard_epoch": 0},
            )
            tool_round_epoch: int | None = None
            raw_pre_tool_text = ""

            async def _capture_tool_round_start() -> None:
                nonlocal tool_round_epoch, raw_pre_tool_text
                current_epoch = int(
                    reply_attempt_state.get("discard_epoch", 0) or 0
                )
                if tool_round_epoch != current_epoch:
                    tool_round_epoch = current_epoch
                    raw_pre_tool_text = "".join(reply_chunks)

            restore_session_prompt = self._apply_turn_memory_context(
                user_session, turn_system_prompt, turn_recalled_text,
                # 私聊 participant 会话的 prompt 也是建会话时烙进去的：
                # 当前轮召回为空不代表旧 prompt 里没有 scoped memory。
                # 每轮都换成刚构建的 prompt，才能让生成内容与本轮依赖快照
                # 对齐；restore 保证持久会话仍保留创建时的原始 system 行。
                always_refresh=(
                    context.is_group
                    or bool(getattr(context, "cross_session_section", ""))
                    or user_data.get("private_memory_mode") == "participant"
                    or not bool(
                        (getattr(self.plugin, "_qq_settings", {}) or {}).get(
                            "allow_cross_group_context", False,
                        )
                    )
                ),
            )
            # recall_memory 工具按轮挂载：群会话是全群共享一个 client，而
            # participant subject 随发言人变——handler 闭包必须在会话锁内按
            # 本轮 context 重建，绝不能在建会话时冻结（那会让所有人都用首
            # 个发言者的 subject）。consent_before 传给闭包：工具读发生在
            # 生成中途，运行时记录要能被生成结束的撤销比对看到。
            armed_turn_tools = self._arm_turn_tools(
                context=context,
                user_session=user_session,
                consent_before=consent_before,
                on_tool_round_start=_capture_tool_round_start,
            )
            generation_completed = False
            try:
                turn_timeout = self.plugin._ai_turn_timeout_seconds
                if armed_turn_tools:
                    # 工具轮的最坏路径是「初始流 + 每次工具迭代各一段流」，
                    # 额度 MAX_TOOL_ITERATIONS 时理论上是 6 段 LLM 流，再加
                    # 各工具自带的预算（召回 5s、检索 16s 含补搜、打开链接
                    # 16s、看一条 B 站视频 30s）。沿用单流预算会把"慢但会
                    # 成功"的工具轮变成超时——而这条路径超时的代价不是丢一
                    # 轮，是丢弃整个共享群会话再打粘性标记。
                    #
                    # 但也不能真的按 6×单轮预算给：正常一段流 10~20 秒，6 段
                    # 两分钟就够；真撞到这个封顶说明模型在空转，让它继续占着
                    # 会话锁（群里就是全群停摆七分钟）比丢弃这一轮更糟。所以
                    # LLM 那部分最多按 TOOL_TURN_STREAM_CAP 段算。
                    turn_timeout = (
                        turn_timeout
                        * min(MAX_TOOL_ITERATIONS + 1, TOOL_TURN_STREAM_CAP)
                        + RECALL_TOOL_HTTP_TIMEOUT_SECONDS
                        + SEARCH_TOOL_HTTP_TIMEOUT_SECONDS
                        + OPEN_TOOL_HTTP_TIMEOUT_SECONDS
                        + WATCH_TIMEOUT_SECONDS
                    )
                await asyncio.wait_for(
                    user_session.stream_text(context.prompt_message),
                    timeout=turn_timeout,
                )

                completed = await self.plugin._wait_session_response_complete(user_session)
                if self._consent_dependency_revoked(context, consent_before):
                    # 生成期间授权被撤销：这条回复的 prompt 里带着已撤销的
                    # 内容，不能送出——清空出站文本只挡住了发送，stream_text
                    # 早已把 ai 行写进共享历史，留着它等于让被撤销的内容既
                    # 进 digest 又进后续轮次的上下文。本轮追加的 ai 行与
                    # tool 轮的裸 dict 行（assistant tool_calls / role=tool，
                    # content 里是召回原文）一并摘掉（human 行是用户自己的
                    # 发言，保留）。
                    self.plugin.logger.warning(
                        f"生成期间记忆授权被撤销，丢弃本轮回复 ({session_key})"
                    )
                    reply_chunks.clear()
                    history = getattr(user_session, "_conversation_history", None)
                    if isinstance(history, list):
                        while (
                            len(history) > history_before
                            and (
                                getattr(history[-1], "type", "") == "ai"
                                or self._is_tool_round_row(history[-1])
                            )
                        ):
                            history.pop()
                if not completed:
                    # 只 raise 不在这里 discard：外层 except TimeoutError 会
                    # 统一走"先抢救群缓冲再丢弃"，这里先 pop 会让 user_data
                    # 在抢救前就没了（原本也是双重 discard）。
                    self.plugin.logger.warning(f"会话 {session_key} 响应超时，关闭并丢弃该会话")
                    raise asyncio.TimeoutError
                generation_completed = True
            finally:
                if armed_turn_tools:
                    # 按轮挂载的对偶收尾：工具与 handler 不得越轮存活——
                    # 同一 client 上的其他生成路径（proactive 的
                    # prompt_ephemeral 等）绝不能带着本轮的 subject 闭包
                    # 发起召回，也不该拿着上一轮的搜索缓存上下文。
                    for clear_slot in (
                        user_session.set_tools,
                        user_session.set_tool_call_handler,
                        getattr(
                            user_session,
                            "set_tool_round_start_callback",
                            None,
                        ),
                    ):
                        if not callable(clear_slot):
                            continue
                        try:
                            clear_slot(None)
                        except Exception:
                            # 单个卸载失败不能阻止另一槽位复位，也不能连累
                            # 下面的历史清理与成员轮记录。
                            pass
                restore_session_prompt()
                history_now = getattr(user_session, "_conversation_history", []) or []
                if isinstance(history_now, list):
                    # tool 轮写进共享历史的裸 dict 行随轮清理：召回原文是按
                    # consent 域临时授权给本轮的，语义与旧管线的"prompt 注入
                    # + restore"一致——留在共享历史里会进 digest、进后续每轮
                    # 的上下文，member 撤销后也无法再摘除。assistant tool-call
                    # 行里的 pre-tool 可见文本先折叠进最终 ai 行，再删掉携带
                    # tool metadata 的裸 dict，保证用户看到的文本与后续上下文
                    # 一致。
                    _, current_pre_tool_text = self._strip_tool_round_rows(
                        history_now,
                        history_before,
                        create_missing_ai_row=generation_completed,
                        outbound_text="".join(reply_chunks),
                        raw_pre_tool_text=(
                            raw_pre_tool_text
                            if tool_round_epoch == int(
                                reply_attempt_state.get(
                                    "discard_epoch", 0,
                                ) or 0
                            )
                            else None
                        ),
                    )
                    user_data["current_pre_tool_text"] = current_pre_tool_text
                appended = list(history_now)[history_before:]
                human_row_materialized = any(
                    getattr(row, "type", "") == "human" for row in appended
                )
                user_data["human_row_accepted"] = human_row_materialized
                # run_primary_session_call consumes human_row_accepted while
                # recording group-member turns. Delivery happens afterward,
                # so preserve separate evidence for reply buffering.
                user_data["human_row_materialized"] = human_row_materialized
                # 本轮真正写进历史的那条 ai 行（没有就是 None）。未投递打标
                # 按它的身份来：用"raw 输出非空"去推断历史里有行，是推断而
                # 不是证据——推断错了就会把上一条**已投递**的回复标成未投递，
                # 那条回复从此再也进不了 digest。
                user_data["current_turn_ai_row"] = next(
                    (row for row in reversed(appended)
                     if getattr(row, "type", "") == "ai"),
                    None,
                )
                self._stamp_nonconsent_boundary(user_data, user_session)

            return "".join(reply_chunks)

    @staticmethod
    def _stamp_nonconsent_boundary(user_data: dict, user_session: Any) -> None:
        """未授权边界在生成轮 finally 记（调用点在 run_primary_session_call）。

        异常/空回复的 human 行也已进历史，只在成功路径记会漏（超时路径
        会话随后被弃，多记无害）。私聊轮同样记（此前限定 is_group）：
        participant 结算分支拿它当 digest 起点地板，OFF 时代的私聊行在
        开关翻 ON 后绝不回溯入库；legacy admin 路径不读该字段，多记无害。
        """  # noqa: DOCSTRING_CJK
        if not user_data.get("memory_enabled"):
            user_data["nonconsent_history_end"] = len(
                getattr(user_session, "_conversation_history", []) or []
            )

    def _arm_turn_tools(
        self,
        *,
        context: Any,
        user_session: Any,
        consent_before: dict,
        on_tool_round_start: Callable[[], Awaitable[None]] | None = None,
    ) -> bool:
        """Install this turn's tools + one dispatching handler.

        三个工具：``recall_memory``（有记忆授权才挂）、``search_web``
        （每次都挂）、``open_url``（每次都挂）。

        为什么搜索要从记忆开关里解耦：原来整段挂载被 ``use_memory_context``
        一刀切，记忆一关连工具通道都没了。搜索和记忆是两件事——记忆关了
        也该能联网。

        为什么三个工具共用一个 handler：客户端只接一个
        ``set_tool_call_handler``，挂两次是后一次覆盖前一次。所以这里装一个
        按 ``tool_call.name`` 分流的闭包，三个工具共用同一个入口。

        Returns whether *any* tool is actually armed. 全都挂不上时返回
        False，调用方的超时预算就按无工具轮来算。
        """
        set_tools = getattr(user_session, "set_tools", None)
        set_handler = getattr(user_session, "set_tool_call_handler", None)
        set_round_start = getattr(
            user_session, "set_tool_round_start_callback", None,
        )
        if not callable(set_tools) or not callable(set_handler):
            # 这一支以前是静默 return False：宿主哪天不再给 session 这个方法，
            # 三处日志都不会留痕，现象只是"工具怎么都不触发"，极难查。留一条
            # 告警，把 session 的类型和两个槽位的有无直接写出来。
            self.plugin.logger.warning(
                f"[工具诊断] 挂载跳过: session={type(user_session).__name__} "
                f"set_tools={'有' if callable(set_tools) else '无'} "
                f"set_tool_call_handler={'有' if callable(set_handler) else '无'}"
            )
            return False

        tools: list[Any] = []
        recall_handler = None
        if getattr(context, "use_memory_context", False):
            try:
                tools.append(
                    self.plugin.memory_tool_service.build_recall_tool_definition()
                )
                recall_handler = self._build_recall_tool_handler(
                    context=context,
                    consent_before=consent_before,
                )
            except Exception as exc:
                self.plugin.logger.warning(
                    f"recall_memory 工具构建失败（本轮无召回）: {exc}"
                )

        search_service = getattr(self.plugin, "search_tool_service", None)
        search_handler = None
        if search_service is not None:
            try:
                tools.append(search_service.build_definition())
                search_handler = self._build_search_tool_handler(search_service)
            except Exception as exc:
                self.plugin.logger.warning(
                    f"search_web 工具构建失败（本轮不联网）: {exc}"
                )

        open_service = getattr(self.plugin, "open_url_service", None)
        open_handler = None
        if open_service is not None:
            try:
                tools.append(open_service.build_definition())
                open_handler = self._build_open_tool_handler(open_service)
                # 抓取额度按轮计：每轮重新挂载时清零。不清的话，聊够几轮
                # 就再也开不了链接了。
                open_service.reset_turn_budget()
            except Exception as exc:
                self.plugin.logger.warning(
                    f"open_url 工具构建失败（本轮不能开链接）: {exc}"
                )

        # B 站见闻：她"自己刷过的视频"。这个工具是纯本地读取（见闻库早就
        # 落盘了），没有网络开销，挂上几乎不增加本轮耗时，所以不必像搜索
        # 那样算进超时预算。
        bilibili_service = getattr(self.plugin, "bilibili_browse_service", None)
        bilibili_handler = None
        if bilibili_service is not None:
            try:
                tools.append(bilibili_service.build_definition())
                bilibili_handler = self._build_bilibili_tool_handler(bilibili_service)
            except Exception as exc:
                self.plugin.logger.warning(
                    f"bilibili_recent 工具构建失败（本轮不提视频）: {exc}"
                )

        # 看别人发来的 B 站链接。这是本轮最重的工具——一条视频 4~5 个请求、
        # 节流后 6~12 秒，所以它单独占一份超时预算（见上面的 turn_timeout）。
        video_service = getattr(self.plugin, "bilibili_video_service", None)
        watch_handler = None
        if video_service is not None:
            try:
                tools.append(video_service.build_definition())
                watch_handler = self._build_bilibili_watch_handler(video_service)
            except Exception as exc:
                self.plugin.logger.warning(
                    f"bilibili_watch 工具构建失败（本轮看不了链接）: {exc}"
                )

        # 表情包：存和发两个工具，共用一个"本轮发过没有"的闸。
        # 一轮只发一张是硬的——群里连着甩三张图就是刷屏，模型自己
        # 控制不住，只能在执行层拦。
        sticker_service = getattr(self.plugin, "sticker_service", None)
        save_sticker_handler = None
        send_sticker_handler = None
        if sticker_service is not None:
            already_sent = [False]
            try:
                tools.append(sticker_service.build_save_definition())
                save_sticker_handler = self._build_sticker_save_handler(
                    sticker_service, context,
                )
            except Exception as exc:
                self.plugin.logger.warning(
                    f"save_sticker 工具构建失败（本轮收不了表情包）: {exc}"
                )
            try:
                tools.append(sticker_service.build_send_definition())
                send_sticker_handler = self._build_sticker_send_handler(
                    sticker_service, context, already_sent,
                )
            except Exception as exc:
                self.plugin.logger.warning(
                    f"send_sticker 工具构建失败（本轮发不了表情包）: {exc}"
                )

        # 图库：翻素材 + 换头像。换头像是她能自己做主的少数几件事之一，
        # 但必须是她自己选——所以给"看"和"换"两个工具，不替她决定。
        gallery_service = getattr(self.plugin, "gallery_service", None)
        browse_gallery_handler = None
        set_avatar_handler = None
        if gallery_service is not None:
            try:
                tools.append(gallery_service.build_browse_definition())
                browse_gallery_handler = self._build_gallery_browse_handler(
                    gallery_service,
                )
            except Exception as exc:
                self.plugin.logger.warning(
                    f"browse_gallery 工具构建失败（本轮翻不了图库）: {exc}"
                )
            try:
                tools.append(gallery_service.build_avatar_definition())
                set_avatar_handler = self._build_avatar_handler(gallery_service)
            except Exception as exc:
                self.plugin.logger.warning(
                    f"set_avatar 工具构建失败（本轮换不了头像）: {exc}"
                )

        # QQ 空间：只给她一把钥匙，不给她日程表。发不发、什么时候发是她
        # 自己的决定，所以这里既不排定时任务、也不在任何事件上自动触发。
        qzone_service = getattr(self.plugin, "qzone_service", None)
        post_qzone_handler = None
        if qzone_service is not None:
            try:
                tools.append(qzone_service.build_post_definition())
                post_qzone_handler = self._build_qzone_post_handler(qzone_service)
                # 宿主重启后第一轮挂载时自检一次（每进程一次），把「凭据」
                # 和「宿主进程到空间接口的连通性」各写一行日志。没有这条，
                # 发表失败时日志里只剩一个 get_cookies，无从判断断在哪。
                if not getattr(qzone_service, "_self_check_attempted", False):
                    try:
                        loop = asyncio.get_running_loop()
                        loop.create_task(qzone_service.ensure_self_check_once())
                    except RuntimeError:
                        # 这里没有运行中的事件循环（工具定义可能在协程外构建）。
                        # 不静默吞：发表前还会再兜底跑一次，但先留一条日志，
                        # 免得以后又被误判成「自检没实现」。
                        self.plugin.logger.info(
                            "空间自检未能在挂载阶段调度（无运行中的事件循环），"
                            "将在首次发表前执行"
                        )
            except Exception as exc:
                self.plugin.logger.warning(
                    f"post_qzone 工具构建失败（本轮发不了空间）: {exc}"
                )

        if not tools:
            self.plugin.logger.warning("[工具诊断] 本轮一个工具都没构建出来")
            return False
        async def _dispatch_tool_call(tool_call: Any) -> ToolResult:
            name = getattr(tool_call, "name", "") or ""
            # handler 真的被宿主回调，是"模型确实发了 tool_calls 且宿主认了"
            # 的硬证据。这条不出现，问题就在模型或宿主那一侧，不在执行侧。
            self.plugin.logger.info(
                f"[工具诊断] 收到工具调用: name={name!r} "
                f"args={getattr(tool_call, 'arguments', None)!r}"
            )
            if name == SEARCH_TOOL_NAME and search_handler is not None:
                return await search_handler(tool_call)
            if name == OPEN_TOOL_NAME and open_handler is not None:
                return await open_handler(tool_call)
            if name == BILIBILI_TOOL_NAME and bilibili_handler is not None:
                return await bilibili_handler(tool_call)
            if name == BILIBILI_WATCH_TOOL_NAME and watch_handler is not None:
                return await watch_handler(tool_call)
            if name == SAVE_TOOL_NAME and save_sticker_handler is not None:
                return await save_sticker_handler(tool_call)
            if name == SEND_TOOL_NAME and send_sticker_handler is not None:
                return await send_sticker_handler(tool_call)
            if name == BROWSE_TOOL_NAME and browse_gallery_handler is not None:
                return await browse_gallery_handler(tool_call)
            if name == AVATAR_TOOL_NAME and set_avatar_handler is not None:
                return await set_avatar_handler(tool_call)
            if name == QZONE_TOOL_NAME and post_qzone_handler is not None:
                return await post_qzone_handler(tool_call)
            if recall_handler is not None:
                return await recall_handler(tool_call)
            return ToolResult(
                call_id=getattr(tool_call, "call_id", "") or "",
                name=name or "unknown",
                output="（没有这个工具）",
            )

        try:
            set_tools(tools)
            set_handler(_dispatch_tool_call)
            if on_tool_round_start is not None and callable(set_round_start):
                set_round_start(on_tool_round_start)
            self.plugin.logger.info(
                f"[工具诊断] 已挂载 {len(tools)} 个: "
                + ", ".join(str(getattr(t, "name", "?")) for t in tools)
            )
            return True
        except Exception as exc:
            self.plugin.logger.warning(f"工具挂载失败（本轮无工具）: {exc}")
            # 各槽位独立做 best-effort 清理：任一个卸载失败都不能阻止
            # 其他槽位复位，否则返回 False 后可能把半挂载状态带到下一轮。
            clear_slots = [set_tools, set_handler]
            if on_tool_round_start is not None and callable(set_round_start):
                clear_slots.append(set_round_start)
            for clear_slot in clear_slots:
                try:
                    clear_slot(None)
                except Exception:
                    # 这里只能 best-effort：原始挂载异常必须保持为本轮降级，
                    # 单个槽位拒绝复位也不能阻止另一槽位继续清理。
                    pass
            return False

    def _build_search_tool_handler(self, search_service: Any):
        """This turn's search_web execution closure.

        一轮只放一次检索：同一轮里模型可能并排发两个 search_web 调用
        （额度 MAX_TOOL_ITERATIONS 下更常见），每个都是一段 4-8 秒的
        宿主 HTTP。第二次直接复用上次输出，不再打接口——这条路径上多
        一次等待就可能顶穿生成侧的超时，代价是丢整个会话。
        """

        last_output: list[str] = []

        async def _handle_search_tool(tool_call: Any) -> ToolResult:
            name = getattr(tool_call, "name", "") or SEARCH_TOOL_NAME
            if last_output:
                self.plugin.logger.info(
                    "search_web 本轮已检索过，复用上次结果"
                )
                output = last_output[0]
            else:
                output = await search_service.execute_search(
                    getattr(tool_call, "arguments", None) or {}
                )
                last_output.append(output)
            return ToolResult(
                call_id=getattr(tool_call, "call_id", "") or "",
                name=name,
                output=output,
            )

        return _handle_search_tool

    def _build_open_tool_handler(self, open_service: Any):
        """This turn's open_url execution closure.

        和 search 那边不同，这里**不**做"本轮已打开过就复用上次输出"：
        模型换一个链接显然是想看那个页面的内容，复用旧结果等于答非所问。

        次数上限放在 service 内部（``_MAX_FETCHES_PER_TURN``）：同一轮里
        模型偶尔会一口气开三四个链接，每个都是几秒的 HTTP，串起来会把
        生成侧的超时预算吃干净。超限时返回一句"先用手上的内容回答"，
        而不是继续等。
        """

        async def _handle_open_tool(tool_call: Any) -> ToolResult:
            name = getattr(tool_call, "name", "") or OPEN_TOOL_NAME
            output = await open_service.execute_open(
                getattr(tool_call, "arguments", None) or {}
            )
            return ToolResult(
                call_id=getattr(tool_call, "call_id", "") or "",
                name=name,
                output=output,
            )

        return _handle_open_tool

    def _build_bilibili_tool_handler(self, bilibili_service: Any):
        """This turn's bilibili_recent closure.

        纯本地读文件，不打网络。这里刻意不缓存：模型一轮里问两次
        「最近看什么」和「有没有猫的」，本来就该拿到不同的切片。
        """

        async def _handle_bilibili_tool(tool_call: Any) -> ToolResult:
            name = getattr(tool_call, "name", "") or BILIBILI_TOOL_NAME
            args = getattr(tool_call, "arguments", None) or {}
            if not isinstance(args, dict):
                args = {}
            try:
                output = bilibili_service.recall_recent(
                    query=str(args.get("query") or ""),
                    limit=int(args.get("limit") or 3),
                )
            except Exception as exc:
                self.plugin.logger.warning(f"bilibili_recent 执行失败: {exc}")
                output = "（翻了一下想不起来，别说具体视频。）"
            return ToolResult(
                call_id=getattr(tool_call, "call_id", "") or "",
                name=name,
                output=output,
            )

        return _handle_bilibili_tool

    def _build_bilibili_watch_handler(self, video_service: Any):
        """This turn's bilibili_watch closure.

        一轮只真看一条。看一条视频要 4~5 个请求、节流后 6~12 秒，模型有时
        会一口气发两个调用（对方贴了两条链接，或者它自己想多看一个），
        第二条再真跑一遍就可能顶穿这一轮的超时——代价是丢掉整条回复。
        所以第二条直接回一句"先记着"，让她嘴上带过去，下一轮再说。
        """

        watched: dict[str, str] = {}

        async def _handle_watch_tool(tool_call: Any) -> ToolResult:
            name = getattr(tool_call, "name", "") or BILIBILI_WATCH_TOOL_NAME
            args = getattr(tool_call, "arguments", None) or {}
            if not isinstance(args, dict):
                args = {}
            raw = str(args.get("url") or args.get("link") or "").strip()
            output = watched.get(raw) if raw else None
            if output is None:
                if watched:
                    output = "这条我先记着了，一次只能看一个视频，这个等下再说。"
                else:
                    try:
                        output = await video_service.execute_watch(args)
                    except Exception as exc:
                        self.plugin.logger.warning(f"bilibili_watch 执行失败: {exc}")
                        output = "这个视频我打不开，你直接告诉我讲了啥吧。"
                    if raw:
                        watched[raw] = output
            return ToolResult(
                call_id=getattr(tool_call, "call_id", "") or "",
                name=name,
                output=output,
            )

        return _handle_watch_tool

    def _build_sticker_save_handler(self, sticker_service: Any, context: Any):
        """本轮的表情包收藏闭包。

        "收哪一张"不从模型拿编号——它看不见序号，只会瞎猜。默认收
        **这条对话里最近收到的图**，这覆盖了绝大多数"这个好玩，存了"的场景。
        """

        async def _handle_save_tool(tool_call: Any) -> ToolResult:
            name = getattr(tool_call, "name", "") or SAVE_TOOL_NAME
            try:
                output = await sticker_service.execute_save(
                    getattr(tool_call, "arguments", None) or {},
                    sender_id=str(getattr(context, "sender_id", "") or ""),
                    group_id=str(getattr(context, "group_id", "") or ""),
                )
            except Exception as exc:
                self.plugin.logger.warning(f"save_sticker 执行失败: {exc}")
                output = "（这张没存下来。别跟对方说你存了。）"
            return ToolResult(
                call_id=getattr(tool_call, "call_id", "") or "",
                name=name,
                output=output,
            )

        return _handle_save_tool

    def _build_sticker_send_handler(
        self, sticker_service: Any, context: Any, already_sent: list[bool],
    ):
        """本轮的表情包发送闭包。

        ``already_sent`` 是跨调用共享的单元素列表：模型在一轮里连发三个
        send_sticker 是常事，第二个开始直接回"别再甩"，不真发。刷屏的
        代价远大于"少发一张"。
        """

        async def _handle_send_tool(tool_call: Any) -> ToolResult:
            name = getattr(tool_call, "name", "") or SEND_TOOL_NAME
            group_id = str(getattr(context, "group_id", "") or "")
            sender_id = str(getattr(context, "sender_id", "") or "")
            try:
                output = await sticker_service.execute_send(
                    getattr(tool_call, "arguments", None) or {},
                    sender_id=sender_id,
                    # 群聊发群里，私聊发给对方。
                    group_id=group_id if getattr(context, "is_group", False) else "",
                    already_sent=already_sent[0],
                )
            except Exception as exc:
                self.plugin.logger.warning(f"send_sticker 执行失败: {exc}")
                output = "（图没发出去。用文字接话，别说你发了。）"
            if output.startswith("发出去了"):
                already_sent[0] = True
            return ToolResult(
                call_id=getattr(tool_call, "call_id", "") or "",
                name=name,
                output=output,
            )

        return _handle_send_tool

    def _build_gallery_browse_handler(self, gallery_service: Any):
        """翻图库。只读，随便翻，不设次数闸。"""

        async def _handle_browse_tool(tool_call: Any) -> ToolResult:
            name = getattr(tool_call, "name", "") or BROWSE_TOOL_NAME
            arguments = getattr(tool_call, "arguments", None) or {}
            query = str(arguments.get("query") or "").strip()
            try:
                limit = int(arguments.get("limit") or 8)
            except (TypeError, ValueError):
                limit = 8
            limit = max(1, min(limit, 20))
            try:
                rows = gallery_service.list_items(query=query, limit=limit)
            except Exception as exc:
                self.plugin.logger.warning(f"browse_gallery 执行失败: {exc}")
                return ToolResult(
                    call_id=getattr(tool_call, "call_id", "") or "",
                    name=name,
                    output="（图库一时打不开）",
                )
            if not rows:
                output = (
                    f"（图库里没有跟“{query}”对得上的）" if query else "（图库还是空的）"
                )
            else:
                lines = []
                for row in rows:
                    tag = "表情" if row.get("kind") == "sticker" else "图"
                    desc = str(row.get("desc") or "").strip() or "（还没描述）"
                    lines.append(f"[{row['id']}]（{tag}）{desc}")
                output = "\n".join(lines)
            return ToolResult(
                call_id=getattr(tool_call, "call_id", "") or "",
                name=name,
                output=output,
            )

        return _handle_browse_tool

    def _build_avatar_handler(self, gallery_service: Any):
        """换头像。失败必须说清楚原因——她要是以为换好了，
        下一句就会跟人聊"你看我新头像"，然后翻车。
        """

        async def _handle_avatar_tool(tool_call: Any) -> ToolResult:
            name = getattr(tool_call, "name", "") or AVATAR_TOOL_NAME
            arguments = getattr(tool_call, "arguments", None) or {}
            key = str(arguments.get("id") or "").strip()
            if not key:
                return ToolResult(
                    call_id=getattr(tool_call, "call_id", "") or "",
                    name=name,
                    output="（没说换哪张。先用 browse_gallery 看一眼，再填编号）",
                )
            try:
                result = await gallery_service.set_avatar(key)
            except Exception as exc:
                self.plugin.logger.warning(f"set_avatar 执行失败: {exc}")
                result = {"ok": False, "reason": f"{type(exc).__name__}"}
            if result.get("ok"):
                output = (
                    f"换好了（{result.get('id')}：{result.get('desc') or '这张'}）。"
                    "头像已经生效，可以顺口跟对方提一句。"
                )
            else:
                output = (
                    f"没换成：{result.get('reason') or '未知原因'}。"
                    "别跟对方说你换了头像。"
                )
            return ToolResult(
                call_id=getattr(tool_call, "call_id", "") or "",
                name=name,
                output=output,
            )

        return _handle_avatar_tool

    def _build_qzone_post_handler(self, qzone_service: Any):
        """发表说说。成败都要如实回给她——说"发了"但其实没发出去，
        是比不发更糟的事：她会接着跟人聊"我刚发了个说说"。
        """

        async def _handle_qzone_tool(tool_call: Any) -> ToolResult:
            name = getattr(tool_call, "name", "") or QZONE_TOOL_NAME
            arguments = getattr(tool_call, "arguments", None) or {}
            content = str(arguments.get("content") or "").strip()
            if not content:
                return ToolResult(
                    call_id=getattr(tool_call, "call_id", "") or "",
                    name=name,
                    output="（内容是空的，没发。想发就把想说的话写进 content）",
                )
            try:
                result = await qzone_service.publish(content)
            except Exception as exc:
                self.plugin.logger.warning(f"post_qzone 执行失败: {exc}")
                result = {"ok": False, "reason": f"{type(exc).__name__}: {exc}"}
            if result.get("ok") and result.get("verified"):
                output = (
                    "发出去了，已经落到你的空间里。"
                    "不用向任何人汇报，也别在聊天里贴一遍内容。"
                )
            elif result.get("ok"):
                output = (
                    "接口回了成功，但我回读没看到这条落地，可能还在同步。"
                    "先别再说自己发了说说，也别立刻再发一条。"
                )
            else:
                output = (
                    f"没发出去：{result.get('reason') or '未知原因'}。"
                    "别跟任何人说你发了说说。"
                )
            return ToolResult(
                call_id=getattr(tool_call, "call_id", "") or "",
                name=name,
                output=output,
            )

        return _handle_qzone_tool

    def _build_recall_tool_handler(
        self, *, context: Any, consent_before: dict,
    ):
        """This turn's recall_memory execution closure.

        Subjects never come from the model: ``execute_recall`` derives
        them from the turn context (the server reads an omitted subjects
        field as the legacy PRIVATE corpus). The closure also owns the
        runtime consent record. Model-authored pre-tool text is ordinary
        assistant output and remains in the outbound buffer.
        """

        # 一轮一次召回的闸在 handler 层：max_tool_iterations 只限 LLM/
        # tool 循环轮数，模型在同一个 assistant 回复里可以并排发多个
        # recall_memory 调用（客户端会逐个执行），流内重试也会再次进
        # tool 轮——每次都是一段 5s HTTP，会击穿超时预算里"一次召回"的
        # 假设，而这条路径超时的代价是丢弃整个共享群会话。空参试探
        # （execute_recall 本就不发 HTTP）不烧额度。
        recall_executed = [False]

        async def _handle_recall_tool(tool_call: Any) -> ToolResult:
            tool_service = self.plugin.memory_tool_service
            arguments = getattr(tool_call, "arguments", None) or {}
            substantive = tool_service.has_recall_arguments(arguments)
            if substantive and recall_executed[0]:
                self.plugin.logger.info(
                    "recall_memory 本轮已执行过，追加调用返回空结果"
                )
                return ToolResult(
                    call_id=getattr(tool_call, "call_id", "") or "",
                    name=getattr(tool_call, "name", "") or "recall_memory",
                    output=tool_service.no_result_text(),
                )
            if substantive:
                recall_executed[0] = True
            output, consumed = await tool_service.execute_recall(
                context=context,
                arguments=arguments,
            )
            if consumed:
                # consent 判据从"prompt 里有没有那段字"换成"运行时有没有
                # 真的发生这次读"：写进本轮的 consent_before（生成结束的
                # 撤销比对读它），并合入 context.consent_snapshot（发送前
                # 与 buffer 的撤销闸读它）。recalled_memory_used 不在这里
                # 记——它跟的是"召回内容被消费"而非"消费了群授权"，私聊
                # legacy 召回 consumed 恒空，由 execute_recall 在回填点记。
                for key, was_enabled in consumed.items():
                    consent_before[key] = (
                        bool(consent_before.get(key)) or bool(was_enabled)
                    )
                self._store_consent_snapshot(context, consumed)
            return ToolResult(
                call_id=getattr(tool_call, "call_id", "") or "",
                name=getattr(tool_call, "name", "") or "recall_memory",
                output=output,
            )

        return _handle_recall_tool

    @staticmethod
    def _is_tool_round_row(row: Any) -> bool:
        """A bare dict row the client's tool loop appended to history.

        Two shapes (OpenAI-compat and genai paths both append these):
        the assistant turn announcing tool_calls, and the role=tool
        result row carrying the recalled text.
        """
        if not isinstance(row, dict):
            return False
        role = row.get("role")
        return role == "tool" or (
            role == "assistant" and bool(row.get("tool_calls"))
        )

    def _strip_tool_round_rows(
        self,
        history: list,
        start_index: int,
        *,
        create_missing_ai_row: bool = False,
        outbound_text: str = "",
        raw_pre_tool_text: str | None = None,
    ) -> tuple[int, str]:
        """Fold visible pre-tool text into the final AI row, then remove tool rows.

        Only rows appended at or after ``start_index`` are considered —
        the rows sit BETWEEN the human row and the final ai row, so this
        scans by index instead of popping from the tail. A repetition
        guard can reset the history to shorter than ``start_index``; the
        range is then empty and nothing is touched.
        """
        start = max(start_index, 0)
        assistant_tool_rows = [
            row for row in history[start:]
            if (
                isinstance(row, dict)
                and row.get("role") == "assistant"
                and row.get("tool_calls")
                and isinstance(row.get("content"), str)
            )
        ]
        persisted_pre_tool_text = "".join(
            str(row.get("content") or "") for row in assistant_tool_rows
        )
        final_ai_row = next(
            (
                row for row in reversed(history[start:])
                if getattr(row, "type", "") == "ai"
            ),
            None,
        )
        final_content = getattr(final_ai_row, "content", None)
        structural_boundary = (
            raw_pre_tool_text is not None or bool(assistant_tool_rows)
        )
        boundary_source = (
            raw_pre_tool_text
            if raw_pre_tool_text is not None
            else persisted_pre_tool_text
        )
        if (
            raw_pre_tool_text is not None
            and len(persisted_pre_tool_text) > len(raw_pre_tool_text)
            and persisted_pre_tool_text.startswith(raw_pre_tool_text)
            and outbound_text.startswith(persisted_pre_tool_text)
        ):
            # Focus 的 thinking stripper 会在 core 发出 tool sentinel 后才
            # flush 被延迟的可见 residual；round-start callback 因而可能只
            # 捕获到真实 prefix 的前半段（最窄情况是空串）。provider 已将
            # 完整 prefix 写入 assistant tool row，可与真实 outbound 对齐时
            # 用它补齐结构边界，仍不修改或丢弃任何模型文本。
            boundary_source = persisted_pre_tool_text
        raw_final_text = ""
        if (
            boundary_source is not None
            and outbound_text.startswith(boundary_source)
        ):
            raw_final_text = outbound_text[len(boundary_source):]
        elif (
            isinstance(final_content, str)
            and final_content
            and outbound_text.endswith(final_content)
        ):
            raw_final_text = final_content

        # QQ 的 sanitizer 对 dangling thinking close tag 是整轮上下文相关的：
        # 单独清洗 prefix 会把本应随 close tag 一起丢弃的内容重新泄进 history。
        # 因此先清洗真实完整出站，再减去同规则清洗后的最终段，所得才是
        # 用户实际可见的结构前缀；内部空白也会原样保留为段间分隔符。
        sanitize = getattr(self.plugin, "_sanitize_generated_reply", None)
        if callable(sanitize):
            visible_outbound = str(sanitize(outbound_text) or "")
            visible_final = str(sanitize(raw_final_text) or "")
        else:
            visible_outbound = outbound_text
            visible_final = raw_final_text
        if visible_final and visible_outbound.endswith(visible_final):
            boundary_pre_tool_text = visible_outbound[:-len(visible_final)]
        elif not visible_final:
            boundary_pre_tool_text = visible_outbound
        else:
            boundary_pre_tool_text = ""

        # 共享历史以 QQ 最终可见的整轮文本为准；只有纯空白时不凭空合成
        # 一条用户没有看到的 AI 行。
        if final_ai_row is not None and not visible_outbound.strip():
            # terminal truncation callback 会先把 raw recovery 写入 history；
            # 若整轮经 QQ sanitizer 后没有任何可见文本，这行既未投递也不
            # 能进入后续上下文/记忆。按对象身份删除，避免误删等值旧行。
            for index in range(len(history) - 1, start - 1, -1):
                if history[index] is final_ai_row:
                    del history[index]
                    break
        elif visible_outbound.strip():
            if final_ai_row is None and create_missing_ai_row:
                history.append(AIMessage(content=visible_outbound))
            elif final_ai_row is not None:
                final_ai_row.content = visible_outbound

        removed = 0
        for index in range(len(history) - 1, start - 1, -1):
            if self._is_tool_round_row(history[index]):
                del history[index]
                removed += 1
        return (
            removed,
            boundary_pre_tool_text
            if structural_boundary and boundary_pre_tool_text.strip()
            else "",
        )

    async def _run_memory_housekeeping(
        self, session_key: str, user_data: dict[str, Any],
    ) -> None:
        """Schedule the backlog / member-bucket drains for this session.

        Shared by the success path and the silent-turn path: the drains are
        the only thing standing between an always-busy group and a queue
        that discards at its hard limit."""
        try:
            await self.plugin._cache_session_delta(session_key, user_data)
        except Exception as exc:
            self.plugin.logger.warning(f"记忆管家调度失败（忽略）: {exc}")

    def _consent_dependency_snapshot(self, context: Any) -> dict:
        """Which consent switches this turn's prompt actually depends on.

        Not group-only: with cross-group consent on, a PRIVATE reply's
        sessions block can name other groups and contacts, so that turn
        depends on the switch too."""
        settings = getattr(self.plugin, "_qq_settings", {}) or {}
        snapshot: dict = {}
        if getattr(context, "cross_session_section", ""):
            snapshot["allow_cross_group_context"] = bool(
                settings.get("allow_cross_group_context", False)
            )
        if not getattr(context, "is_group", False):
            if getattr(context, "participant_memory_enabled", False) and (
                getattr(context, "core_memory_text", "")
                or getattr(context, "recalled_memory_text", "")
            ):
                # 私聊 participant 轮的 prompt 依赖该开关（对偶群轮的
                # group_memory_enabled 记账）：发送前撤销复检要覆盖它。
                snapshot["private_participant_memory_enabled"] = bool(
                    settings.get("private_participant_memory_enabled", False)
                )
            return snapshot
        if getattr(context, "core_memory_text", "") or getattr(
            context, "recalled_memory_text", "",
        ):
            snapshot["group_memory_enabled"] = bool(
                settings.get("group_memory_enabled", False)
            )
            if getattr(context, "used_member_subject", False):
                snapshot["group_member_memory_enabled"] = bool(
                    settings.get("group_member_memory_enabled", False)
                )
        if getattr(context, "cross_group_section", ""):
            snapshot["allow_cross_group_context"] = bool(
                settings.get("allow_cross_group_context", False)
            )
        if getattr(context, "owner_private_section", ""):
            snapshot["cross_domain_owner_memory"] = bool(
                settings.get("cross_domain_owner_memory", False)
            )
        if getattr(context, "owner_longterm_section", ""):
            # 长期记忆与最近对话是两个开关：只关掉长期记忆的用户，那一段
            # 也不能带着旧事发到群里。
            snapshot["cross_domain_owner_longterm_memory"] = bool(
                settings.get("cross_domain_owner_longterm_memory", False)
            )
        return snapshot

    def _store_consent_snapshot(self, context: Any, snapshot: dict) -> None:
        """Carry the generation-time snapshot to the pre-send gate.

        Unions with whatever the context already carries: a nested
        synthetic turn inherits the buffered drafts' dependencies, and its
        own (clean) prompt must not erase them."""
        try:
            merged = dict(getattr(context, "consent_snapshot", None) or {})
            for key, was_enabled in (snapshot or {}).items():
                merged[key] = bool(merged.get(key)) or bool(was_enabled)
            context.consent_snapshot = merged
        except Exception:
            # 合成调用方可能传的是轻量对象：拿不到就退回"发送前不复检"，
            # 生成后的复检仍在。
            pass

    def _consent_dependency_revoked(self, context: Any, before: dict) -> bool:
        """True when a switch this prompt relied on went off since `before`."""
        if not before:
            return False
        settings = getattr(self.plugin, "_qq_settings", {}) or {}
        return any(
            was_enabled and not settings.get(key, False)
            for key, was_enabled in before.items()
        )

    def _sanitize_for_live_consent(
        self, context: Any, system_prompt: str, recalled_text: str,
    ) -> tuple[str, str]:
        """Drop prompt sections whose consent is no longer live.

        One place for all three switches so every generation path (primary
        session call, direct fallback) enforces the same boundary — the
        per-path rechecks kept diverging as new paths appeared."""
        settings = getattr(self.plugin, "_qq_settings", {}) or {}
        if not settings.get("allow_cross_group_context", False):
            # 私聊轮也可能带会话清单段（跨群开关打开时它会列出其他群与
            # 联系人）：非群轮不能在这里直接返回，否则那段撤不掉。
            system_prompt = self._strip_section_text(
                system_prompt, getattr(context, "cross_session_section", "") or "",
            )
        if not getattr(context, "is_group", False):
            if getattr(
                context, "participant_memory_enabled", False,
            ) and not settings.get("private_participant_memory_enabled", False):
                # 私聊 participant 轮的授权在生成前被撤销：scoped 召回与
                # bootstrap 段全部撤除（对偶下面群分支的撤法）。
                recalled_text = ""
                system_prompt = self._strip_section_text(
                    system_prompt,
                    getattr(context, "core_memory_text", "") or "",
                )
            return system_prompt, recalled_text
        core_text = getattr(context, "core_memory_text", "") or ""
        if not settings.get("group_memory_enabled", False):
            # 群记忆关闭：scoped 召回与 bootstrap 段全部撤除。
            recalled_text = ""
            system_prompt = self._strip_section_text(system_prompt, core_text)
        elif getattr(context, "used_member_subject", False) and not settings.get(
            "group_member_memory_enabled", False,
        ):
            # 仅 member 关闭：召回混合了群域与 participant 域、无法事后
            # 拆分，连同 participant 派生的 bootstrap 段一起撤除。
            recalled_text = ""
            system_prompt = self._strip_section_text(system_prompt, core_text)
        if not settings.get("allow_cross_group_context", False):
            system_prompt = self._strip_section_text(
                system_prompt, getattr(context, "cross_group_section", "") or "",
            )
        if not settings.get("cross_domain_owner_memory", False):
            # 主人私聊→群的注入段：与跨群段同窗口复检（开关关掉后，群里
            # 这条回复不得再带着私聊原文发出去）。
            system_prompt = self._strip_section_text(
                system_prompt, getattr(context, "owner_private_section", "") or "",
            )
        if not settings.get("cross_domain_owner_longterm_memory", False):
            # 长期记忆段同理，但它有自己的开关：两段各撤各的，不能因为
            # 只关了长期记忆就把最近聊到的几句也一并抹掉。
            system_prompt = self._strip_section_text(
                system_prompt, getattr(context, "owner_longterm_section", "") or "",
            )
        return system_prompt, recalled_text

    @staticmethod
    def _strip_section_text(system_prompt: str, section_text: str) -> str:
        """Remove one composed section (with its separator) from a prompt."""
        if not section_text or section_text not in system_prompt:
            return system_prompt
        separator = "\n\n"
        for candidate in (
            separator + section_text, section_text + separator, section_text,
        ):
            if candidate in system_prompt:
                return system_prompt.replace(candidate, "", 1)
        return system_prompt

    @staticmethod
    def _strip_scoped_sections(system_prompt: str, context: Any) -> str:
        """Remove scoped-memory sections from an already-composed prompt.

        Used when group memory is revoked between context construction and
        generation: the bootstrap section is the only scoped block left in
        the prompt (recall is passed separately and simply dropped)."""
        return QQReplyGenerationService._strip_section_text(
            system_prompt, getattr(context, "core_memory_text", "") or "",
        )

    def _apply_turn_memory_context(
        self, user_session: Any, system_prompt: str, recalled_memory_text: str,
        *, always_refresh: bool = False,
    ):
        # always_refresh：群轮即使无召回也要换 prompt——
        # _compose_turn_instructions 会自动省略空的召回段，swap 退化为
        # 纯 system_prompt 替换；restore 保证会话落盘的仍是创建时原文。
        if not recalled_memory_text and not always_refresh:
            return lambda: None
        conversation_history = getattr(user_session, "_conversation_history", None)
        if not conversation_history or not isinstance(conversation_history[0], SystemMessage):
            return lambda: None
        original_system_message = conversation_history[0]
        original_instructions = getattr(user_session, "_instructions", original_system_message.content)
        enhanced_instructions = self._compose_turn_instructions(system_prompt, recalled_memory_text)
        conversation_history[0] = SystemMessage(content=enhanced_instructions)
        user_session._instructions = enhanced_instructions

        def restore() -> None:
            current_history = getattr(user_session, "_conversation_history", None)
            if current_history and current_history[0] is not original_system_message:
                current_history[0] = original_system_message
            user_session._instructions = original_instructions

        return restore

    async def _sync_memory_after_success(
        self,
        *,
        session_key: str,
        user_data: dict[str, Any],
        context: QQReplyContext,
        reply_text: str = "",
    ) -> None:
        if user_data.get("memory_enabled"):
            try:
                # member turn 已在主生成完成点单点记录（含空回复轮）；此处
                # 只做 cache 同步，避免同一发言重复入 bucket。
                # 合成私聊轮（主动搭话）的回复直接发送、不经 buffer，尾部
                # ai 行当轮即为定局，和指令行一起送去过滤——指令被排除名单
                # 挡掉，她说出口的话照常入库。普通轮仍滞后一拍等投递决策。
                count = await self.plugin._cache_session_delta(
                    session_key, user_data,
                    sync_tail_ai=(
                        not context.is_group
                        and is_synthetic_source(getattr(context, "source_kind", ""))
                    ),
                )
                if count:
                    self.plugin.logger.info(f"[管理员] 成功同步 {count} 条消息到 Memory Server (会话: {session_key})")
            except Exception as e:
                self.plugin.logger.error(f"记忆同步失败: {e}")
            # mention 计数不在这里记：本钩子跑在生成成功时刻，buffer 可能
            # 把这条回复截停并用 summary 取代——没投递的草稿不得推进
            # suppression 计数。投递点统一调 record_scoped_mentions_on_delivery。
            return

        if user_data.get("memory_context_used"):
            self.plugin.logger.info(f"[临时发送] 已使用记忆上下文但跳过记忆同步 (会话: {session_key})")
            return
        if context.is_group:
            # 未授权边界已在 run_primary_session_call 每次尝试后统一记录。
            self.plugin.logger.info(f"[群聊] 跳过记忆同步 (群: {context.group_id}, 用户: {context.sender_id})")
            return
        self.plugin.logger.info(f"[非管理员] 跳过记忆同步 (用户: {context.sender_id}, 权限: {context.permission_level})")

    def append_fallback_ai_row(
        self, context: QQReplyContext, reply_text: str,
    ) -> None:
        """Put a delivered direct-fallback reply into the shared history.

        The primary session accepted the human row but produced nothing, so
        the fallback's text exists only in the outbound message: without
        this a scoped digest persists a one-sided conversation and loses
        whatever the bot disclosed. Idempotent — the row is tagged so a
        second delivery hook cannot double-append."""
        is_group = bool(getattr(context, "is_group", False))
        is_participant = bool(
            not is_group
            and getattr(context, "participant_memory_enabled", False)
        )
        if not (is_group or is_participant) or not reply_text:
            return
        if getattr(context, "ephemeral_session", False):
            return
        session_key = self.plugin.session_runtime_service.build_generation_session_key(context)
        user_data = (getattr(self.plugin, "_user_sessions", {}) or {}).get(
            session_key
        )
        if not user_data or not user_data.get("memory_enabled"):
            return
        if (
            is_participant
            and user_data.get("private_memory_mode") != "participant"
        ):
            return
        session = user_data.get("session")
        history = getattr(session, "_conversation_history", None)
        if history is None:
            return
        # 幂等键取本轮消息 ID：context 对象可能被重建（构造时的 turn_uid
        # 就变了），而重复的投递钩子未必紧挨着——扫最近几行也会漏，故全
        # 历史精确匹配。没有消息 ID 的轮次（主动发言/合成轮/平台缺字段）
        # 退到 context 构造时生成的 turn_uid：绝不能用 id(context)，地址
        # 复用会让后续每一条 fallback 行都被误判成重复而永久丢失。
        turn_id = (
            str(getattr(context, "current_message_id", "") or "")
            or str(getattr(context, "turn_uid", "") or "")
        )
        if not turn_id:
            history.append(self._build_fallback_row(reply_text, ""))
            return
        marker = f"fallback:{turn_id}"
        for msg in reversed(history):
            if getattr(msg, "type", "") == "ai" and (
                getattr(msg, "additional_kwargs", None) or {}
            ).get("neko_fallback_row") == marker:
                return
        history.append(self._build_fallback_row(reply_text, marker))

    @staticmethod
    def _build_fallback_row(reply_text: str, marker: str):
        try:
            from langchain_core.messages import AIMessage

            row = AIMessage(content=reply_text)
            row.additional_kwargs["neko_fallback_row"] = marker
            return row
        except Exception:
            return SimpleNamespace(
                type="ai", content=reply_text,
                additional_kwargs={"neko_fallback_row": marker},
            )

    async def record_scoped_mentions_on_delivery(
        self, context: QQReplyContext, reply_text: str,
    ) -> None:
        """Bump scoped mention counters when a reply is ACTUALLY delivered.

        群路径绕开 legacy post_turn，scoped 条目的 mention 计数（防重复注入
        的 suppression 输入）只能在插件侧补记——且必须绑定投递而非生成：
        buffer 合并场景的草稿没人看到，各记一次会把被引用条目推进 suppression
        阈值、错误地从后续上下文消失。best-effort：失败只影响该条目晚几轮
        进入"暂不主动提及"。"""
        if not reply_text or context.ephemeral_session:
            return
        if not context.is_group:
            await self._record_participant_mentions_on_delivery(
                context, reply_text,
            )
            return
        if not (getattr(self.plugin, "_qq_settings", {}) or {}).get(
            "group_memory_enabled", False,
        ):
            # mention 计数是对群域记忆元数据的写：开关关掉之后不得再改，
            # 哪怕会话侧的 flag 还没被后台结算清掉。
            return
        session_key = self.plugin.session_runtime_service.build_generation_session_key(context)
        user_data = self.plugin._user_sessions.get(session_key)
        if not user_data or not user_data.get("memory_enabled"):
            return
        await self._record_scoped_mentions_best_effort(context, reply_text)

    async def _record_participant_mentions_on_delivery(
        self, context: QQReplyContext, reply_text: str,
    ) -> None:
        """私聊 participant 轮的 mention 计数（对偶群路径）。

        没有它，scoped 条目的防重复 suppression 对私聊 participant 永不
        生效，模型会在每次回复里重复提起同一条事实。legacy admin 私聊走
        本体 post_turn，不在这里记。"""
        if not getattr(context, "participant_memory_enabled", False):
            return
        if not (getattr(self.plugin, "_qq_settings", {}) or {}).get(
            "private_participant_memory_enabled", False,
        ):
            # 与群分支同语义：开关关掉之后不得再改 participant 域元数据。
            return
        session_key = self.plugin.session_runtime_service.build_generation_session_key(context)
        user_data = self.plugin._user_sessions.get(session_key)
        if not user_data or not user_data.get("memory_enabled"):
            return
        sender_id = str(context.sender_id or "").strip()
        if not sender_id or is_synthetic_source(
            getattr(context, "source_kind", ""),
        ):
            # 合成轮的名义 sender 不是真实对话方；缺 sender 时 fail-closed
            # ——绝不退化成无 subject 的 legacy 写。
            return
        try:
            await self.plugin.memory_bridge.post_scoped_mentions(
                context.her_name, reply_text,
                subjects=[
                    self.plugin.memory_bridge.participant_subject(sender_id),
                ],
            )
        except Exception as e:
            self.plugin.logger.warning(f"participant mention 记录失败（忽略）: {e}")

    async def _record_scoped_mentions_best_effort(
        self, context: QQReplyContext, reply_text: str,
    ) -> None:
        """Bump scoped mention counters with the subjects this reply was
        authorized to see, so repeatedly-volunteered scoped entries reach the
        suppression threshold like legacy entries do."""
        group_id = str(context.group_id or "").strip()
        if not group_id:
            return
        bridge = self.plugin.memory_bridge
        subjects = [bridge.group_subject(group_id)]
        sender_id = str(context.sender_id or "").strip()
        synthetic = is_synthetic_source(getattr(context, "source_kind", ""))
        member_authorized = bool(
            getattr(context, "member_memory_enabled", False)
            and (getattr(self.plugin, "_qq_settings", {}) or {}).get(
                "group_member_memory_enabled", False,
            )
        )
        if sender_id and not synthetic and member_authorized:
            # 合成轮的名义 sender 不是真实发言人——mention 计数只按群域记，
            # 与召回/写入侧的合成轮过滤对齐。member 未授权时也不记：该域
            # 本轮没被召回，扫描/改写留存条目会把没展示过的事实压进
            # suppression、之后 opt-in 也不再出现。
            subjects.append(bridge.group_participant_subject(group_id, sender_id))
        try:
            await bridge.post_scoped_mentions(
                context.her_name, reply_text, subjects=subjects,
            )
        except Exception as e:
            self.plugin.logger.warning(f"scoped mention 记录失败（忽略）: {e}")

    async def run_fallback_memory_hooks(
        self, context: QQReplyContext, fallback_reply: str,
    ) -> None:
        """fallback 成功也要跑 scoped 记忆钩子：成员发言入 bucket、被展示
        的 scoped 条目计 mention——主会话空回复不代表这轮没发生。生产
        pipeline 走 QQReplyModelNode.generate()，legacy 入口走
        generate_from_context()——两条 fallback 成功路径都必须调这里。
        会话可能已被超时丢弃（user_data 不在了则跳过）。"""
        if not context.is_group or context.ephemeral_session:
            # ephemeral 键含 time_ns，重新生成必 miss；且 ephemeral 会话
            # persist=False、finally 即丢弃，记忆钩子本就无意义。
            return
        session_key = self.plugin.session_runtime_service.build_generation_session_key(context)
        user_data = self.plugin._user_sessions.get(session_key)
        if user_data is not None:
            await self._sync_memory_after_success(
                session_key=session_key, user_data=user_data,
                context=context, reply_text=fallback_reply,
            )

    async def generate_from_context(self, context: QQReplyContext) -> QQModelResult:
        if not context.is_group and context.permission_level not in ["admin", "trusted"]:
            return QQModelResult(reply_text=None, source="none")

        primary_result = await self.run_primary_session_call(context)
        if not primary_result.allow_fallback:
            return primary_result

        fallback_reply = await self.generate_fallback_from_context(context)
        if fallback_reply:
            primary_result.traces.append(
                QQPipelineStageTrace(
                    stage="model_fallback",
                    status="success",
                    metadata={"reply_length": len(fallback_reply), "group_scene_mode": context.group_scene_mode},
                )
            )
            await self.run_fallback_memory_hooks(context, fallback_reply)
            return QQModelResult(reply_text=fallback_reply, source="direct_llm_fallback", used_fallback=True, traces=primary_result.traces)
        primary_result.traces.append(
            QQPipelineStageTrace(
                stage="model_fallback",
                status="empty",
                metadata={"reply_length": 0, "group_scene_mode": context.group_scene_mode},
            )
        )
        return QQModelResult(reply_text=None, source="none", used_fallback=True, traces=primary_result.traces)

    async def generate_reply(
        self,
        message: str,
        permission_level: str,
        sender_id: str,
        attachments: list[dict[str, Any]] | None = None,
        is_group: bool = False,
        group_id: str = None,
        user_nickname: Optional[str] = None,
        use_memory_context: Optional[bool] = None,
        persist_memory: Optional[bool] = None,
        ephemeral_session: bool = False,
        group_facing: bool = False,
        group_scene_mode: str = "",
    ) -> Optional[str]:
        context = await self.plugin.reply_context_node.build(
            message=message,
            permission_level=permission_level,
            sender_id=sender_id,
            attachments=attachments,
            is_group=is_group,
            group_id=group_id,
            user_nickname=user_nickname,
            use_memory_context=use_memory_context,
            persist_memory=persist_memory,
            ephemeral_session=ephemeral_session,
            group_facing=group_facing,
            group_scene_mode=group_scene_mode,
        )
        model_result = await self.generate_from_context(context)
        outcome = self.plugin.reply_postprocess_node.finalize(context, model_result)
        return outcome.reply_text
