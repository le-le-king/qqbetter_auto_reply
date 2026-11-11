
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, Dict, Optional

from plugin.sdk.plugin import Err, NekoPluginBase, Ok, SdkError, lifecycle, neko_plugin, plugin_entry, tr, ui

try:
    from utils.tts.native_voice_registry import get_active_realtime_native_provider_for_ui
except (ImportError, ModuleNotFoundError):
    get_active_realtime_native_provider_for_ui = None
try:
    from utils.tts.providers.gemini import normalize_gemini_tts_voice
except (ImportError, ModuleNotFoundError):
    normalize_gemini_tts_voice = None
try:
    from utils.voice_clone import (
        MimoVoiceCloneClient,
        MimoVoiceCloneError,
        MinimaxVoiceCloneClient,
        MinimaxVoiceCloneError,
    )
except (ImportError, ModuleNotFoundError):
    MimoVoiceCloneClient = MimoVoiceCloneError = MinimaxVoiceCloneClient = MinimaxVoiceCloneError = None
try:
    from utils.voice_config import read_legacy_voice_id
except (ImportError, ModuleNotFoundError):
    read_legacy_voice_id = None

from .attention_gate_service import QQAttentionGateService
from .attention_service import QQAttentionService
from .backlog_service import QQBacklogService
from .backlog_store import QQBacklogStore
from .bilibili_browse_service import QQBilibiliBrowseService
from .bilibili_login_service import QQBilibiliLoginService
from .bilibili_video import QQBilibiliVideoService
from .catchup_service import QQMessageCatchupService
from .config_store import QQAutoReplyConfigStore
from .dashboard_service import QQDashboardService
from .display_name_service import QQDisplayNameService
from .face_index import QQFaceIndex
from .fatigue_service import QQFatigueService
from .gallery_service import QQGalleryService
from .group_permission import GroupPermissionManager
from .handler_runtime_service import QQHandlerRuntimeService
from .memory_bridge import QQMemoryBridge
from .memory_tool_service import QQMemoryToolService
from .message_dispatcher import QQMessageDispatcher
from .napcat_service import QQNapcatService
from .open_url_service import QQOpenUrlToolService
from .permission import PermissionManager
from .proactive_private_service import QQProactivePrivateService
from .prompt_builder import QQPromptBuilder
from .prompting import QQAutoReplyPromptingMixin
from .qq_client import QQClient
from .relay_service import QQRelayService
from .reply_buffer_service import QQReplyBufferService
from .reply_context_node import QQReplyContextNode
from .reply_decision_node import QQReplyDecisionNode
from .reply_delivery_node import QQReplyDeliveryNode
from .reply_generation_service import QQReplyGenerationService
from .reply_model_node import QQReplyModelNode
from .reply_pipeline import QQReplyPipelineRunner
from .reply_postprocess_node import QQReplyPostprocessNode
from .reply_relay_node import QQReplyRelayNode
from .runtime_ops_service import QQProactiveMessageService, QQRuntimeOpsService
from .runtime_service import QQRuntimeService
from .scheduled_greeting_service import QQScheduledGreetingService
from .search_tool_service import QQSearchToolService
from .session import QQAutoReplySessionMixin
from .session_bootstrap_service import QQSessionBootstrapService
from .session_instruction_service import (
    QQSessionInstructionService,
    resolve_prompt_override,
)
from .session_memory_service import QQSessionMemoryService
from .session_runtime_service import QQSessionRuntimeService
from .settings_service import QQSettingsService
from .sticker_service import QQStickerService
from .targets import QQAutoReplyTargetsMixin
from .vision_service import QQVisionService
from .voice_reply_service import QQVoiceReplyService


def build_open_ui_payload(*, plugin_id: str, available: bool, i18n=None) -> dict[str, Any]:
    path = f"/plugin/{plugin_id}/ui/" if available else ""
    message_key = "ui.open_path.message" if available else "ui.unavailable.message"
    default_message = "UI 已注册" if available else "UI 未注册"
    message = i18n.t(message_key, default=default_message) if i18n else default_message
    return {
        "available": available,
        "path": path,
        "message": message,
    }


@neko_plugin
class QQAutoReplyPlugin(QQAutoReplySessionMixin, QQAutoReplyPromptingMixin, QQAutoReplyTargetsMixin, NekoPluginBase):
    SESSION_IDLE_TIMEOUT_SECONDS = 300
    SESSION_SWEEP_INTERVAL_SECONDS = 30
    LOG_BUFFER_SIZE = 500

    def __init__(self, ctx):
        super().__init__(ctx)
        self.file_logger = self.enable_file_logging(log_level="INFO")
        self.logger = self.file_logger
        # 内存日志缓冲区（供前端运行日志页读取）
        import collections
        import time as _time
        self._log_buffer: collections.deque = collections.deque(maxlen=self.LOG_BUFFER_SIZE)
        self._last_log_push_at = 0.0
        self._log_push_throttle_seconds = 1.5
        self._last_status_push_at = 0.0
        self._status_push_throttle_seconds = 2.0
        def _emit(level: str, msg: str) -> None:
            try:
                ts = _time.strftime("%H:%M:%S")
                self._log_buffer.append(f"{ts} [{level}] {msg}")
            except Exception:
                pass
            self._maybe_push_log_event()
        self._emit_log = _emit
        self.config_store = QQAutoReplyConfigStore(self.data_path())
        self._qq_settings: dict[str, Any] = self.config_store.default_config()
        self.backlog_store = self._create_backlog_store_from_settings(self._qq_settings)
        self.settings_service = QQSettingsService(self)
        self.runtime_service = QQRuntimeService(self)
        self.dashboard_service = QQDashboardService(self)
        self.napcat_service = QQNapcatService(self)
        self.backlog_service = QQBacklogService(self)
        self.fatigue_service: Optional[QQFatigueService] = None
        self.attention_service = QQAttentionService(self)
        self.prompt_builder = QQPromptBuilder(self)
        self.memory_bridge = QQMemoryBridge(self)
        self.display_name_service = QQDisplayNameService(self)
        self.memory_tool_service = QQMemoryToolService(self)
        self.search_tool_service = QQSearchToolService(self)
        self.open_url_service = QQOpenUrlToolService(self)
        self.relay_service = QQRelayService(self)
        self.reply_generation_service = QQReplyGenerationService(self)
        self.reply_decision_node = QQReplyDecisionNode(self)
        self.reply_context_node = QQReplyContextNode(self)
        self.reply_model_node = QQReplyModelNode(self)
        self.reply_postprocess_node = QQReplyPostprocessNode(self)
        self.reply_delivery_node = QQReplyDeliveryNode(self)
        self.reply_buffer_service: Optional[QQReplyBufferService] = None
        self.reply_relay_node = QQReplyRelayNode(self)
        self.reply_pipeline = QQReplyPipelineRunner(self)
        self.voice_reply_service = QQVoiceReplyService(self)
        self.runtime_ops_service = QQRuntimeOpsService(self)
        self.proactive_message_service = QQProactiveMessageService(self)
        # 断线补消息：OneBot 是纯推送、没有重放，断线期间的事件会直接消失。
        # 这个服务记住每个会话处理到哪条（message_seq），重连后回去和服务端
        # 历史对账，把漏掉的补回正常管道。
        self.catchup_service = QQMessageCatchupService(self)
        self.proactive_private_service = QQProactivePrivateService(self)
        # 定时搭话：主人提前定「几点说什么」，到点模型自由发挥发私聊。
        # 依赖 proactive_message_service（构造顺序在它之后）。
        self.scheduled_greeting_service = QQScheduledGreetingService(self)
        # B 站见闻：登录服务要先生成（刷取服务每轮从它这里同步登录态）
        self.bilibili_login_service = QQBilibiliLoginService(self)
        self.bilibili_browse_service = QQBilibiliBrowseService(self)
        # 看别人发来的链接。放在 browse 之后：它借 browse 的 client 共用
        # 节流和登录态，构造时就要能拿到。
        self.bilibili_video_service = QQBilibiliVideoService(self)
        # 看图：QQ 里发的照片和表情包要能"看见"。走宿主 conversation 模型，
        # 和猫娘本体识图同一个配置，只是多了按内容缓存和表情包专用问法。
        self.vision_service = QQVisionService(self)
        # QQ 表情索引。图本体留在腾讯服务器，这里只存 emoji_id / key 这些标识；
        # 描述按需生成一次（收藏时），之后永久复用——这是省钱的关键。
        self.face_index = QQFaceIndex(self, self.vision_service)
        # 表情包库：存 + 按心情找 + 甩出去。表情包优先走 face_index（零成本），
        # 普通图片才落盘。
        self.sticker_service = QQStickerService(self, self.vision_service, self.face_index)
        # 图库：把她见过的图和表情包连描述一起收进 Q 盘，供检索、当头像。
        # 和 sticker_service 的分工：那个管「她要发什么」，这个管「她见过什么」。
        self.gallery_service = QQGalleryService(self)
        self.handler_runtime_service = QQHandlerRuntimeService(self)
        self.message_dispatcher = QQMessageDispatcher(self)
        self.session_bootstrap_service = QQSessionBootstrapService(self)
        self.session_instruction_service = QQSessionInstructionService(self)
        self.session_memory_service = QQSessionMemoryService(self)
        self.session_runtime_service = QQSessionRuntimeService(self)
        self.qq_client: Optional[QQClient] = None
        self.attention_gate_service = QQAttentionGateService(self)
        self.permission_mgr: Optional[PermissionManager] = None
        self.group_permission_mgr: Optional[GroupPermissionManager] = None
        self._running = False
        self._message_task: Optional[asyncio.Task] = None
        self._session_housekeeping_task: Optional[asyncio.Task] = None
        self._group_digest_task: Optional[asyncio.Task] = None
        self._trust_migration_task: Optional[asyncio.Task] = None
        self._identity_scope_task: Optional[asyncio.Task] = None
        # 只有在存量 trust 池成功推给 memory_server 之后才开始上报
        # speaker_tier / speaker_activity_events。纵深防御第一层，服务端的
        # legacy_barriers 是第二层。
        self.trust_ready: asyncio.Event = asyncio.Event()
        self._handler_tasks: set[asyncio.Task] = set()
        self._user_sessions: dict[str, dict[str, Any]] = {}
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._session_locks_guard = asyncio.Lock()
        self._message_concurrency = asyncio.Semaphore(3)
        self._max_concurrent_messages = 3
        self._ai_connect_timeout_seconds = 10.0
        self._ai_turn_timeout_seconds = 60.0
        self._handler_shutdown_timeout_seconds = 10.0
        self._normal_relay_probability = 0.1
        self._truth_reply_probability = 0.1
        self._admin_qq: Optional[str] = None
        self._strategy_mode: str = "neko_dynamic"
        self._napcat_process: Optional[asyncio.subprocess.Process] = None
        self._manages_napcat_process = False
        self._proactive_task: Optional[asyncio.Task] = None
        self._last_proactive_enabled = False
        self._last_proactive_send_at = 0.0
        self._last_proactive_greeting_at = 0.0
        self._backlog_summary_threshold = 10
        self._backlog_notify_cooldown_seconds = 900
        self._backlog_issue_notify_threshold = 1
        self._relay_backlog_items: list[dict[str, Any]] = []
        self._recent_pipeline_traces: list[dict[str, Any]] = []
        self._poke_timestamps: dict[str, list[float]] = {}  # user_id → 最近回戳时间戳列表（5分钟窗口）
        self._poke_storm: dict[str, list[tuple[float, str]]] = {}  # group_id → [(timestamp, poker_id)] 戳猫娘风暴检测
        self._startup_error: str | None = None

    def _create_backlog_store_from_settings(self, settings: dict[str, Any] | None) -> QQBacklogStore:
        return QQBacklogStore(
            self.data_path(),
            retention_limit=int((settings or {}).get("backlog_retention_limit", 200) or 200),
        )

    def _make_qq_connection(self):
        # 延迟导入：qq_client/qq_open_plat 是重模块（顶层 import 会拖慢插件进程
        # 启动握手），而连接对象只在真正启动自动回复时才需要。
        mode = str((self._qq_settings or {}).get("qq_connection_mode", "napcat") or "napcat").strip()
        if mode == "open_platform":
            from .qq_open_plat import QQOpenPlatformConnection
            return QQOpenPlatformConnection(
                app_id=str((self._qq_settings or {}).get("qq_open_app_id") or "").strip(),
                client_secret=str((self._qq_settings or {}).get("qq_open_client_secret") or "").strip(),
                logger=self.logger,
                # 每条事件现读，开关改完立刻生效，不必重连。
                identity_probe=lambda: bool(
                    (self._qq_settings or {}).get("qq_open_identity_probe_enabled", False)
                ),
                # 取证行要同时进 UI 的「运行日志」页，不能只落文件。
                emit_log=self._emit_log,
            )
        return QQClient(
            onebot_url=str((self._qq_settings or {}).get("onebot_url") or "ws://0.0.0.0:6199"),
            token=str((self._qq_settings or {}).get("token") or ""),
            logger=self.logger,
            emit_log=self._emit_log,
            image_describer=self._describe_reply_image,
            voice_transcriber=self._transcribe_voice,
            # napcat_forward = 正向 WS 客户端（主动拨出到 OneBot 实现方的 WS 服务器）；
            # 其余（含默认）走反向 WS 服务器。
            direction="forward" if mode == "napcat_forward" else "reverse",
        )

    # ── UI SSE 事件推送（#2822 通道）──────────────────────────

    def _spawn_push_ui_event(self, msg_type: str, text: str = "") -> None:
        """把一次 SSE 推送放进事件循环，fire-and-forget——绝不打断消息管线。

        所有调用点都在插件事件循环内（消息处理/生命周期协程），get_running_loop
        可用；推失败静默（SSE 是尽力而为，前端有兜底轮询）。
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._push_ui_event(msg_type, text))

    async def _push_ui_event(self, msg_type: str, text: str = "") -> None:
        """经 /ui-api/push 向插件 UI 的 SSE 客户端广播一条事件。

        用进程级回环 HTTP 单例 + 固定插件服务基址；只发本插件的 channel。
        """
        try:
            from config import USER_PLUGIN_BASE
            from utils.internal_http_client import get_internal_http_client

            client = get_internal_http_client()
            await client.post(
                f"{USER_PLUGIN_BASE}/plugin/{self.plugin_id}/ui-api/push",
                json={"type": msg_type, "text": str(text or msg_type)[:200]},
            )
        except Exception:
            pass

    def _maybe_push_log_event(self) -> None:
        """日志写入后的节流推送（1.5s 内最多一次 {"type":"logs"}），避免每条日志一条 SSE。"""
        try:
            import time as _t
            now = _t.time()
            if now - self._last_log_push_at < self._log_push_throttle_seconds:
                return
            self._last_log_push_at = now
            self._spawn_push_ui_event("logs")
        except Exception:
            pass

    def _maybe_push_status_event(self) -> None:
        """高频状态变更的节流推送（2s 内最多一次 {"type":"status"}）。

        消息活动/注意力离散事件/缓冲变更都会调这里；运行翻转这种低频但重要
        的状态用直接 ``_spawn_push_ui_event("status")``，不走节流。
        """
        try:
            import time as _t
            now = _t.time()
            if now - self._last_status_push_at < self._status_push_throttle_seconds:
                return
            self._last_status_push_at = now
            self._spawn_push_ui_event("status")
        except Exception:
            pass

    async def _transcribe_voice(self, audio_base64: str = "", *, audio_url: str = "") -> str:
        """语音转文字：优先本地 STT，其次云端 OpenAI/Qwen。audio_url 用于 Qwen。"""
        try:
            import base64 as b64

            import httpx
            from utils.config_manager import get_config_manager

            core_config = get_config_manager().get_core_config() or {}
            audio_bytes = b64.b64decode(audio_base64) if audio_base64 else b""
            # 如果没有 base64 但有 URL，下载音频
            if not audio_bytes and audio_url:
                try:
                    async with httpx.AsyncClient(timeout=30.0, proxy=None, trust_env=False) as cl:
                        dl = await cl.get(audio_url)
                        if dl.status_code == 200:
                            audio_bytes = dl.content
                except Exception:
                    pass

            stt_filename = "voice.mp3"
            stt_mime = "audio/mp3"

            # ── 本地 STT（优先独立配置 local_stt_url，其次 tts_custom base_url 推导）──
            if audio_bytes:
                try:
                    amr_detected = audio_bytes and (audio_bytes[:6] == b"#!AMR\n" or audio_bytes[:9].startswith(b"#!AMR-W"))
                    stt_filename = "voice.amr" if amr_detected else "voice.mp3"
                    stt_mime = "audio/amr" if amr_detected else "audio/mp3"
                    # 优先使用 qq_settings 中的 local_stt_url
                    local_stt_url = str((self._qq_settings or {}).get("local_stt_url", "") or "").strip()
                    if not local_stt_url:
                        # 回退：tts_custom base_url 推导
                        tts_config = get_config_manager().get_model_api_config("tts_custom")
                        local_base = str(tts_config.get("base_url") or "").strip()
                        _is_ws = local_base.startswith("ws://") or local_base.startswith("wss://")
                        _is_http = local_base.startswith("http://") or local_base.startswith("https://")
                        if local_base and (_is_ws or _is_http):
                            http_base = local_base.replace("ws://", "http://").replace("wss://", "https://")
                            local_stt_url = http_base.rstrip("/") + "/v1/audio/transcriptions"
                    if local_stt_url:
                        async with httpx.AsyncClient(timeout=30.0, proxy=None, trust_env=False) as client:
                            resp = await client.post(
                                local_stt_url,
                                files={"file": (stt_filename, audio_bytes, stt_mime)},
                                data={"model": "whisper-1", "language": "zh"},
                            )
                            if resp.status_code == 200:
                                text = str(resp.json().get("text", "") or "").strip()
                                if text:
                                    self._emit_log("INFO", f"[Voice] 本地STT完成: {text[:40]}")
                                    return text
                            self._emit_log("DEBUG", f"[Voice] 本地STT: {resp.status_code}")
                except Exception:
                    pass

            # ── OpenAI Whisper ──
            openai_key = str(core_config.get("ASSIST_API_KEY_OPENAI") or "").strip()
            if openai_key and audio_bytes:
                async with httpx.AsyncClient(timeout=30.0, proxy=None, trust_env=False) as client:
                    resp = await client.post(
                        "https://api.openai.com/v1/audio/transcriptions",
                        headers={"Authorization": f"Bearer {openai_key}"},
                        files={"file": (stt_filename, audio_bytes, stt_mime)},
                        data={"model": "whisper-1", "language": "zh"},
                    )
                    if resp.status_code == 200:
                        return str(resp.json().get("text", "") or "").strip()
                    self._emit_log("DEBUG", f"[Voice] OpenAI转录: {resp.status_code}")

            # ── Qwen DashScope (SenseVoice) 同步模式 ──
            import json as _json
            qwen_key = str(core_config.get("ASSIST_API_KEY_QWEN") or "").strip()
            if qwen_key and audio_bytes:
                amr_detected = audio_bytes and (audio_bytes[:6] == b"#!AMR\n" or audio_bytes[:9].startswith(b"#!AMR-W"))
                if amr_detected:
                    self._emit_log("DEBUG", f"[Voice] 检测到AMR: magic={audio_bytes[:9]!r}")
                self._emit_log("DEBUG", f"[Voice] Qwen同步转录: {len(audio_bytes)} bytes")
                mime = "audio/amr-wb" if (amr_detected and audio_bytes[:9].startswith(b"#!AMR-W")) else ("audio/amr" if amr_detected else "audio/mpeg")
                data_uri = f"data:{mime};base64,{b64.b64encode(audio_bytes).decode()}"
                async with httpx.AsyncClient(timeout=30.0, proxy=None, trust_env=False) as client:
                    submit_resp = await client.post(
                        "https://dashscope.aliyuncs.com/api/v1/services/audio/asr/transcription",
                        headers={"Authorization": f"Bearer {qwen_key}"},
                        json={
                            "model": "sensevoice-v1",
                            "input": {"file_urls": [data_uri]},
                        },
                    )
                    if submit_resp.status_code != 200:
                        try:
                            err = submit_resp.json()
                            self._emit_log("DEBUG", f"[Voice] Qwen同步转录失败: {submit_resp.status_code} code={err.get('code','?')} msg={err.get('message','?')}")
                        except Exception:
                            self._emit_log("DEBUG", f"[Voice] Qwen同步转录失败: {submit_resp.status_code} {submit_resp.text[:200]}")
                    else:
                        result = submit_resp.json()
                        output = result.get("output") or {}
                        results = output.get("results") or []
                        text = ""
                        for r in results:
                            transcripts = r.get("transcripts") or []
                            trans_url = r.get("transcription_url") or ""
                            if not transcripts and trans_url:
                                try:
                                    async with httpx.AsyncClient(timeout=15.0, proxy=None, trust_env=False) as dl:
                                        dl_resp = await dl.get(trans_url)
                                    if dl_resp.status_code == 200:
                                        trans_data = dl_resp.json()
                                        transcripts = trans_data.get("transcripts") or []
                                        props = trans_data.get("properties") or {}
                                        if props:
                                            dur = props.get("original_duration_in_milliseconds", 0)
                                            fmt = props.get("audio_format", "?")
                                            sr = props.get("original_sampling_rate", 0)
                                            self._emit_log("DEBUG", f"[Voice] Qwen音频属性: {fmt} {sr}Hz {dur}ms")
                                    else:
                                        self._emit_log("DEBUG", f"[Voice] Qwen下载转录失败: status={dl_resp.status_code}")
                                except Exception as e:
                                    self._emit_log("DEBUG", f"[Voice] Qwen下载转录异常: {type(e).__name__}: {e}")
                            if transcripts:
                                import re as _re
                                for t in transcripts:
                                    raw_text = str(t.get("text", "") or "").strip()
                                    if raw_text:
                                        cleaned = _re.sub(r'<\|/?\w+\|>', '', raw_text).strip()
                                        text += cleaned
                            else:
                                text += str(r.get("transcript", "") or r.get("text", "") or "").strip()
                            result_text = text.strip()
                            if result_text:
                                self._emit_log("INFO", f"[Voice] Qwen转录完成: {result_text[:80]}")
                            else:
                                self._emit_log("DEBUG", f"[Voice] Qwen转录成功但无文字, full_output={_json.dumps(output, ensure_ascii=False)[:2000]}")
                            return result_text
            return ""
        except Exception:
            return ""

    async def _describe_reply_image(self, image_url: str, *, kind: str = "image") -> str:
        """描述一张图 / 表情包。``kind="sticker"`` 时问法换成"什么情绪"。

        实现在 ``vision_service``：同一张图（同一 URL 或同一份字节）只花一次
        VLM 调用。群里一张梗图一天能被转二十次，每次都问一遍纯浪费。
        """
        vision = getattr(self, "vision_service", None)
        if vision is None:
            return ""
        return await vision.describe(image_url, kind=kind)

    def _refresh_admin_qq(self) -> None:
        self._admin_qq = None
        if not self.permission_mgr:
            return
        for user in self.permission_mgr.list_users():
            if user.get("level") == "admin":
                qq = str(user.get("qq") or "").strip()
                if qq:
                    self._admin_qq = qq
                    return

    def _get_reply_mode(self) -> str:
        return self.config_store.normalize_reply_mode((self._qq_settings or {}).get("reply_mode"))

    def _get_voice_output_dir(self) -> Path:
        return self.voice_reply_service.get_voice_output_dir()

    async def _cleanup_voice_output_dir(self, *, max_age_seconds: int = 1800) -> None:
        await self.voice_reply_service.cleanup_voice_output_dir(max_age_seconds=max_age_seconds)

    async def _get_current_voice_id(self) -> str:
        return await self.voice_reply_service.get_current_voice_id()

    async def _synthesize_reply_voice_audio(self, text: str) -> tuple[bytes, str]:
        return await self.voice_reply_service.synthesize_reply_voice_audio(text)

    async def _synthesize_reply_voice_file(self, text: str) -> tuple[str, str]:
        return await self.voice_reply_service.synthesize_reply_voice_file(text)

    async def _deliver_private_reply(self, target_qq: str, text: str, *, voice_text: str = "", fallback_to_text_on_voice_failure: bool) -> bool:
        return await self.voice_reply_service.deliver_private_reply(
            target_qq,
            text,
            voice_text=voice_text,
            fallback_to_text_on_voice_failure=fallback_to_text_on_voice_failure,
        )

    async def _deliver_group_reply(self, group_id: str, text: str, *, reply_message_id: str = "", at_user_id: str = "", keyboard: str = "", voice_text: str = "", fallback_to_text_on_voice_failure: bool) -> bool:
        return await self.voice_reply_service.deliver_group_reply(
            group_id,
            text,
            reply_message_id=reply_message_id,
            at_user_id=at_user_id,
            keyboard=keyboard,
            voice_text=voice_text,
            fallback_to_text_on_voice_failure=fallback_to_text_on_voice_failure,
        )

    async def _load_business_config(self) -> dict[str, Any]:
        return await self.settings_service.load_business_config()

    async def _ensure_business_config_initialized(self) -> dict[str, Any]:
        return await self.settings_service.ensure_business_config_initialized()

    async def _create_business_config(self) -> dict[str, Any]:
        return await self.settings_service.create_business_config()

    async def _persist_business_config(self) -> bool:
        return await self.settings_service.persist_business_config()

    async def _mutate_business_config(self, mutation) -> bool:
        """Route direct action mutations through the serialized writer."""
        settings_service = getattr(self, "settings_service", None)
        if settings_service is not None:
            return await settings_service.mutate_business_config(mutation)
        # Preserve the established lightweight-host seam used by unit tests.
        if not mutation(self._qq_settings):
            return True
        return await self._persist_business_config()

    def _ensure_qq_client_initialized(self) -> None:
        if self.qq_client is not None:
            return
        self.qq_client = self._make_qq_connection()
        self._bind_catchup_hooks()

    def _bind_catchup_hooks(self) -> None:
        """把断线补消息挂到 QQ 客户端上。

        只在客户端刚建出来时调一次。``add_*_hook`` 内部按回调对象去重，
        重复绑不会重复触发；所以就算以后客户端被换掉，重新调一次也安全。
        """
        client = self.qq_client
        if client is None:
            return
        # 表情包索引和补消息互不依赖，先各自绑上（补消息服务缺席不该连带着
        # 表情包也收不了）。
        self._bind_face_hook()
        catchup = getattr(self, "catchup_service", None)
        if catchup is None:
            return
        try:
            client.add_connected_hook(catchup.on_connected)
            client.add_message_hook(catchup.note_message)
        except Exception:
            # 补消息挂不上不该拦着插件启动——大不了退化成"断线就丢"
            self.logger.warning("断线补消息钩子注册失败", exc_info=True)

    def _bind_face_hook(self) -> None:
        """收到表情包就往索引里记一笔。

        挂在消息钩子上而不是等模型调用工具，是因为**这一刻才拿得到 key**：
        QQ 每次上报都带一份新的发送凭据，隔一会儿就过期。先记下来，之后
        她要不要发、什么时候发，都不影响。

        这个钩子是同步的、只写内存，不下载图、不调模型 —— 收藏表情包 0 成本。
        """
        client = self.qq_client
        faces = getattr(self, "face_index", None)
        if client is None or faces is None:
            return

        def _remember(message: Dict[str, Any]) -> None:
            try:
                segments = message.get("message")
                if not isinstance(segments, list):
                    return
                sender_id = str(message.get("user_id") or "")
                group_id = str(message.get("group_id") or "")
                for segment in segments:
                    faces.remember(segment, sender_id=sender_id, group_id=group_id)
            except Exception:
                self.logger.debug("表情包索引记录失败", exc_info=True)

        try:
            client.add_message_hook(_remember)
        except Exception:
            self.logger.warning("表情包索引钩子注册失败", exc_info=True)

    @lifecycle(id="startup")
    async def startup(self, **_):
        if not await self.config_store.exists():
            await self._create_business_config()
        settings = await self._ensure_business_config_initialized()
        self.settings_service.rebuild_permission_managers(settings)
        self.settings_service.apply_runtime_settings(settings)
        await self.attention_service.load_cached_state()
        self.fatigue_service = QQFatigueService(self)
        self.reply_buffer_service = QQReplyBufferService(self)
        self._ensure_qq_client_initialized()
        # 游标要在连接建立**之前**加载好：连接的瞬间就会触发一次补消息对账，
        # 那时如果游标还是空的，她会被当成"从没见过这个人"。
        try:
            await self.catchup_service.start()
        except Exception:
            self.logger.warning("断线补消息服务启动失败", exc_info=True)
        # 表情包库跟 QQ 连接无关：索引在磁盘上，启动就读，不必等连上。
        try:
            await self.vision_service.start()
        except Exception:
            self.logger.warning("识图缓存启动失败", exc_info=True)
        try:
            await self.face_index.start()
        except Exception:
            self.logger.warning("QQ 表情索引启动失败", exc_info=True)
        try:
            await self.sticker_service.start()
        except Exception:
            self.logger.warning("表情包库启动失败", exc_info=True)
        try:
            await self.gallery_service.start()
        except Exception:
            self.logger.warning("图库启动失败", exc_info=True)
        if self.attention_gate_service:
            await self.attention_gate_service.start_proactive_loop()
        # B 站见闻跟 QQ 连接无关：就算没连 OneBot，她的"刷视频"也该继续
        # 积累。所以挂在 startup，不挂在 start_auto_reply 里。
        bilibili_browse = getattr(self, "bilibili_browse_service", None)
        if bilibili_browse:
            try:
                await bilibili_browse.start()
            except Exception:
                # 刷不了不该拖垮插件启动
                self.logger.warning("B站见闻循环启动失败", exc_info=True)
        # UI 静态文件不设强缓存（默认 max-age=3600 会让浏览器缓存旧版
        # script.js/index.html 长达 1 小时，改代码后用户仍看到旧页面、
        # 报早已修复的错误）。no-cache = 每次加载都重新校验，内容恒为最新。
        self.register_static_ui("static", cache_control="no-cache")
        self.set_list_actions([
            {
                "id": "open_ui",
                "label": self.i18n.t("ui.actions.open", default="打开 UI"),
                "kind": "ui",
                "target": f"/plugin/{self.plugin_id}/ui/",
                "open_in": "new_tab",
            }
        ])
        # 后台推送存量 trust 池，**不阻塞 startup**：memory_server 可能还没
        # 起来，而在 startup 里 await 一个带退避的重试循环既拖慢插件启动、
        # 又是在赌另一个进程的就绪顺序。
        if (
            self._trust_migration_task is None
            or self._trust_migration_task.done()
        ):
            self._trust_migration_task = asyncio.create_task(
                self.settings_service.push_legacy_speaker_trust_forever()
            )
        # 标识符语义的登记**不在这里**：它描述的是「现在跑着的 wire
        # format」，而 startup 时还没有连接。登记发生在连接真正建立之后
        # （runtime_ops_service 的 start_auto_reply，见 §2.15.4）。
        if self._session_housekeeping_task is None or self._session_housekeeping_task.done():
            self._session_housekeeping_task = asyncio.create_task(self._session_housekeeping_loop())
        # 定期清理已审核超过24h的旧消息
        if getattr(self, "_purge_task", None) is None or self._purge_task.done():
            self._purge_task = asyncio.create_task(self._purge_old_reviewed_loop())
        return Ok({"status": "ready"})

    async def _purge_old_reviewed_loop(self):
        """每小时清理一次已审核超过 24 小时的旧消息。"""
        await asyncio.sleep(300)  # 启动后等 5 分钟再开始
        while True:
            try:
                removed = await self.backlog_store.purge_old_reviewed(max_age_seconds=86400)
                if removed > 0:
                    self._emit_log("INFO", f"清理了 {removed} 条过期已审核消息")
            except asyncio.CancelledError:
                break
            except Exception:
                self.logger.warning("清理过期消息失败", exc_info=True)
            await asyncio.sleep(3600)

    async def _group_digest_loop(self, interval_minutes: int = 5):
        """定期将各群聊摘要推送到 Memory Server（跨群共享记忆）"""
        await asyncio.sleep(60)
        while True:
            try:
                await asyncio.sleep(interval_minutes * 60)
            except asyncio.CancelledError:
                break
            try:
                sessions = getattr(self, "_user_sessions", {}) or {}
                for key, s in list(sessions.items()):
                    if not isinstance(s, dict) or not s.get("is_group"):
                        continue
                    session = s.get("session")
                    if not session or not hasattr(session, "_conversation_history"):
                        continue
                    history = getattr(session, "_conversation_history", []) or []
                    if len(history) < 4:
                        continue
                    group_id = str(s.get("group_id") or key)
                    her_name = str(s.get("her_name") or "neko")
                    login_id = str(s.get("login_self_id") or "")
                    sender_id = str(s.get("sender_id") or "")
                    user_title = str(s.get("user_title") or "")
                    user_label = f"{user_title}(QQ:{sender_id})" if user_title else f"QQ{sender_id}"
                    messages = []
                    for msg in history[-20:]:
                        role = getattr(msg, "role", "") if hasattr(msg, "role") else msg.get("role", "")
                        content = getattr(msg, "content", "") if hasattr(msg, "content") else msg.get("content", "")
                        if role in ("user", "assistant") and content:
                            messages.append({"role": role, "content": str(content)[:200]})
                    if not messages:
                        continue
                    try:
                        await self.memory_bridge.post_memory_history(
                            "process",
                            her_name,
                            [{"role": "system", "content": (
                                f"[QQ群聊记录] {her_name} 使用QQ插件在群 {group_id}"
                                + (f"（账号 {login_id}）" if login_id else "")
                                + " 聊了以下内容：\n"
                                + "\n".join(f"{user_label if m['role']=='user' else her_name}: {m['content']}" for m in messages[-8:])
                            )}],
                            timeout=3.0,
                        )
                        self._emit_log("INFO", f"群 {group_id} 摘要已推送 Memory Server ({len(messages)}条)")
                    except Exception:
                        pass
            except Exception as e:
                self.logger.warning(f"群摘要推送异常: {e}")

    @lifecycle(id="shutdown")
    async def shutdown(self, **_):
        if self.attention_gate_service:
            await self.attention_gate_service.stop_proactive_loop()
        bilibili_browse = getattr(self, "bilibili_browse_service", None)
        if bilibili_browse:
            try:
                await bilibili_browse.stop()
            except Exception:
                self.logger.warning("B站见闻循环停止失败", exc_info=True)
        # 表情包索引是攒着写的，关机必须落一次，否则刚收的几张会丢。
        vision = getattr(self, "vision_service", None)
        if vision:
            try:
                await vision.stop()
            except Exception:
                self.logger.warning("识图缓存停止失败", exc_info=True)
        faces = getattr(self, "face_index", None)
        if faces:
            try:
                await faces.stop()
            except Exception:
                self.logger.warning("QQ 表情索引停止失败", exc_info=True)
        sticker = getattr(self, "sticker_service", None)
        if sticker:
            try:
                await sticker.stop()
            except Exception:
                self.logger.warning("表情包库停止失败", exc_info=True)
        # 图库索引也是攒着写的（去抖 3 秒），关机前必须落一次。
        gallery = getattr(self, "gallery_service", None)
        if gallery:
            try:
                await gallery.flush()
            except Exception:
                self.logger.warning("图库落盘失败", exc_info=True)
        await self._stop_auto_reply_runtime(stop_napcat=True)
        sync_tasks = (
            list(getattr(self, "_group_memory_sync_tasks", ()) or ())
            + list(getattr(self, "_prompt_change_discard_tasks", ()) or ())
        )
        if sync_tasks:
            # 隐私关键的开关转变任务在关机 flush 前 join（限 1s），避免
            # 结算做到一半被进程退出截断。
            # asyncio.wait 不取消未完成任务——超时放行但不杀结算。
            done_tasks, _pending = await asyncio.wait(sync_tasks, timeout=1.0)
            for finished in done_tasks:
                if finished.cancelled():
                    self._emit_log("WARNING", "记忆同步任务被外部取消")
                    continue
                exc = finished.exception()
                if exc is not None:
                    self._emit_log("ERROR", f"记忆同步任务异常结束: {exc}")
        await self._flush_all_memory_sessions(reason="shutdown")
        if self.attention_gate_service:
            await self.attention_gate_service.shutdown()
        if (
            self._trust_migration_task
            and not self._trust_migration_task.done()
        ):
            self._trust_migration_task.cancel()
        if (
            self._identity_scope_task
            and not self._identity_scope_task.done()
        ):
            self._identity_scope_task.cancel()
        if self._group_digest_task and not self._group_digest_task.done():
            self._group_digest_task.cancel()
        if getattr(self, "_purge_task", None) and not self._purge_task.done():
            self._purge_task.cancel()
        if self._session_housekeeping_task:
            self._session_housekeeping_task.cancel()
            try:
                await self._session_housekeeping_task
            except asyncio.CancelledError:
                pass
            self._session_housekeeping_task = None
        # 这里不关 http client：记忆桥与附件下载用的是 utils/http 的进程级
        # 单例，由 main_server 的 shutdown 钩子统一关。插件自己关会把上面
        # 那批"只 join 1s、不取消"的结算任务的在途请求打断。
        return Ok({"status": "shutdown"})

    def _mask_token(self, token: str) -> str:
        normalized = str(token or "")
        if not normalized:
            return ""
        if len(normalized) <= 6:
            return "*" * len(normalized)
        return f"{normalized[:3]}***{normalized[-3:]}"

    def _get_napcat_directory(self) -> Path:
        return self.napcat_service.get_napcat_directory()

    def _get_napcat_launch_target(self) -> Path:
        return self.napcat_service.get_napcat_launch_target()

    def _get_napcat_qrcode_path(self) -> Path:
        return self.napcat_service.get_napcat_qrcode_path()

    async def _sync_napcat_qrcode_into_static(self) -> bool:
        return await self.napcat_service.sync_napcat_qrcode_into_static()

    def _resolve_sticker_path(self, sticker_id: str) -> str:
        """解析表情包 ID 到文件路径（供 delivery_node 使用）。"""
        return self.reply_pipeline._resolve_sticker_path(sticker_id)

    def _sticker_paths(self) -> tuple[str, str]:
        """表情包目录 + 索引路径。

        一律用 ``data_path()``（和其他插件数据同一处）。以前这几个入口拼的是
        ``__file__`` 相对路径，图会写进**安装目录**，插件更新一次全丢，
        而且和收藏功能所在的目录不是同一个。
        """
        import os
        base = str(self.data_path())
        return os.path.join(base, "sticker"), os.path.join(base, "sticker.json")

    def _find_napcat_launcher(self) -> Path | None:
        return self.napcat_service.find_napcat_launcher()

    async def _ensure_napcat_started(self) -> None:
        mode = str((self._qq_settings or {}).get("qq_connection_mode", "napcat") or "napcat").strip()
        if mode == "open_platform":
            return  # 开放平台不需要本地 NapCat 进程
        # 反向和正向都需要本地 NapCat：正向模式同样是 NapCat 提供 QQ 登录/
        # 扫码与 OneBot 服务，只是我们作为 WS 客户端拨出而不是等它拨入。
        await self.napcat_service.ensure_napcat_started()

    async def _stop_managed_napcat(self) -> None:
        await self.napcat_service.stop_managed_napcat()

    def _build_runtime_status(self) -> dict[str, Any]:
        return self.runtime_service.build_runtime_status()

    async def _fetch_login_status_payload(self) -> dict[str, Any]:
        return await self.runtime_service.fetch_login_status_payload()

    async def _refresh_actual_contacts_cache(self) -> dict[str, Any]:
        return await self.runtime_service.refresh_actual_contacts_cache()

    async def _build_dashboard_state(self) -> dict[str, Any]:
        return await self.dashboard_service.build_dashboard_state()

    @ui.context(id="qqbetter_auto_reply")
    async def get_dashboard_context(self):
        return await self.dashboard_service.build_dashboard_context()

    async def open_ui(self, **_):
        return await self.dashboard_service.open_ui()

    @ui.action(label=tr("ui.onboarding.step3.init"), refresh_context=True)
    @plugin_entry(
        id="init_config",
        name=tr("entries.init_config.name", default="初始化 QQ 配置"),
        description=tr("entries.init_config.description", default="在第一次使用 QQ 插件、完成引导或缺少配置文件时，创建一份新的 QQ 配置。"),
        input_schema={"type": "object", "properties": {"guide_step_config_done": {"type": "boolean"}}, "additionalProperties": False},
    )
    async def init_config(self, guide_step_config_done: Optional[bool] = None, **_):
        return await self.dashboard_service.init_config(guide_step_config_done=guide_step_config_done)

    @plugin_entry(
        id="configure_onebot_nl",
        name=tr("entries.configure_onebot_nl.name", default="用自然语言配置 OneBot 连接"),
        description=tr("entries.configure_onebot_nl.description", default="通过自然语言描述来设置或修改 OneBot 的 WebSocket 地址和 Access Token。例如：设置地址为 ws://0.0.0.0:6199 token 为 abc123、把 OneBot 地址改成 ws://192.168.1.1:3001、清空 token"),
        input_schema={"type": "object", "properties": {"message": {"type": "string", "description": "自然语言指令"}}, "required": ["message"], "additionalProperties": False},
    )
    async def configure_onebot_nl(self, message: str = "", **_):
        """通过自然语言解析并保存 OneBot 配置"""
        import re
        text = str(message or "").strip()
        if not text:
            return Err(SdkError("INVALID_INPUT: 请提供自然语言指令，如：设置地址为 ws://0.0.0.0:6199 token 为 abc123"))

        url = ""
        token = ""
        clear_token = False

        # 提取 WebSocket/HTTP 地址
        url_patterns = [
            r'(wss?://\S+)',           # ws://... 或 wss://...
            r'(https?://\S+)',          # http://... 或 https://...
            r'地址[设为是]*[：:\s]*(\S+:\d+\S*)',  # 地址设为 xxx:3001/...
            r'url[设为是]*[：:\s]*(\S+:\d+\S*)',   # url 设为 ...
            r'改为\s*(\S+:\d+\S*)',    # 改为 ...
            r'改成\s*(\S+:\d+\S*)',    # 改成 ...
        ]
        for pattern in url_patterns:
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                candidate = m.group(1).rstrip(".,;!?）)")
                if "://" in candidate:
                    url = candidate
                    break

        # 提取 token
        token_patterns = [
            r'token\s*[设为是]*[：:\s]*(\S+)',     # token 设为 xxx
            r'access_token\s*[设为是]*[：:\s]*(\S+)',
            r'密钥\s*[设为是]*[：:\s]*(\S+)',
            r'token\s*[=：:]\s*(\S+)',
        ]
        for pattern in token_patterns:
            m = re.search(pattern, text, re.IGNORECASE)
            if m:
                candidate = m.group(1).rstrip(".,;!?）)")
                if candidate in ("空", "无", "清空", "清除", "none", "null"):
                    clear_token = True
                else:
                    token = candidate
                break

        # 检测清空 token
        if not token and not clear_token:
            if re.search(r'(清空|清除|去掉|删除|移除)\s*token', text, re.IGNORECASE):
                clear_token = True

        if not url and not token and not clear_token:
            return Ok({
                "parsed": False,
                "hint": "未能从指令中解析出 OneBot 地址或 Token。请尝试更明确的表达，如：设置地址为 ws://0.0.0.0:6199，token 为 my_token_123",
                "current": {
                    "onebot_url": str(self._qq_settings.get("onebot_url", "")),
                    "token_configured": bool(self._qq_settings.get("token")),
                },
            })

        # 构建 save_settings 参数
        save_kwargs: dict[str, Any] = {}
        if url:
            save_kwargs["onebot_url"] = url
        if token:
            save_kwargs["token"] = token
        if clear_token:
            save_kwargs["token"] = ""

        await self.dashboard_service.save_settings(**save_kwargs)

        changes: list[str] = []
        if url:
            changes.append(f"地址 → {url}")
        if token:
            changes.append(f"Token → {self._mask_token(token)}")
        if clear_token:
            changes.append("Token → (已清空)")

        return Ok({
            "parsed": True,
            "changes": changes,
            "reconnect_required": bool(self._running),
            "hint": "配置已保存" + ("，需要重启自动回复以应用新连接" if self._running else ""),
        })

    @plugin_entry(id="get_dashboard_state", name=tr("entries.get_dashboard_state.name", default="获取控制面板状态"), description=tr("entries.get_dashboard_state.description", default="读取 QQ 插件当前的运行状态、登录状态、联系人数量、配置项和引导进度。"), input_schema={"type": "object", "properties": {}})
    async def get_dashboard_state(self, **_):
        return await self.dashboard_service.get_dashboard_state()

    @ui.action(id="refresh_actual_contacts", label=tr("entries.refresh_actual_contacts.name", default="刷新实际联系人列表"), refresh_context=True)
    @plugin_entry(id="refresh_actual_contacts", name=tr("entries.refresh_actual_contacts.name", default="刷新实际联系人列表"), description=tr("entries.refresh_actual_contacts.description", default="重新从 OneBot 拉取 QQ 好友和群聊列表，用于更新联系人显示。"), input_schema={"type": "object", "properties": {}})
    async def refresh_actual_contacts(self, **_):
        return await self.dashboard_service.refresh_actual_contacts()

    @plugin_entry(
        id="upload_sticker",
        name=tr("entries.upload_sticker.name", default="上传表情包"),
        description=tr("entries.upload_sticker.description", default="上传一张图片 base64 数据，自动保存到 data/sticker/ 目录并注册到 sticker.json。"),
        input_schema={"type": "object", "properties": {"filename": {"type": "string", "description": "文件名（如 cat.png）"}, "data_base64": {"type": "string", "description": "图片 base64 编码数据"}, "desc": {"type": "string", "description": "表情包描述"}}, "required": ["filename", "data_base64", "desc"], "additionalProperties": False},
        metadata={"timeout": 30},
    )
    async def upload_sticker(self, filename: str = "", data_base64: str = "", desc: str = "", **_):
        """上传表情包图片并注册"""
        import base64 as b64
        import json as _json
        import os as _os
        fname = str(filename or "").strip()
        description = str(desc or "").strip()
        raw_b64 = str(data_base64 or "").strip()
        if not fname:
            return Err(SdkError("INVALID_INPUT: filename 不能为空"))
        if not raw_b64:
            return Err(SdkError("INVALID_INPUT: data_base64 不能为空"))
        if not description:
            return Err(SdkError("INVALID_INPUT: desc 不能为空"))
        sticker_dir, sticker_json = self._sticker_paths()
        _os.makedirs(sticker_dir, exist_ok=True)
        # 处理 base64（可能带 data:image/...;base64, 前缀）
        if "," in raw_b64 and raw_b64.startswith("data:"):
            raw_b64 = raw_b64.split(",", 1)[1]
        # 安全检查：文件名只保留安全字符
        safe_name = "".join(c for c in fname if c.isalnum() or c in "._-")
        if not safe_name:
            safe_name = "sticker.png"
        # 避免重名
        base, ext = _os.path.splitext(safe_name)
        if not ext:
            ext = ".png"
        dest_name = safe_name
        counter = 1
        while _os.path.exists(_os.path.join(sticker_dir, dest_name)):
            dest_name = f"{base}_{counter}{ext}"
            counter += 1
        dest_path = _os.path.join(sticker_dir, dest_name)
        try:
            img_bytes = b64.b64decode(raw_b64)
        except Exception as e:
            return Err(SdkError(f"DECODE_FAILED: base64 解码失败: {e}"))
        with open(dest_path, "wb") as f:
            f.write(img_bytes)
        # 注册到 sticker.json
        try:
            with open(sticker_json, "r", encoding="utf-8") as f:
                data = _json.loads(f.read())
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        next_id = 1
        while str(next_id) in data:
            next_id += 1
        sid = str(next_id)
        data[sid] = {"desc": description, "path": dest_name}
        with open(sticker_json, "w", encoding="utf-8") as f:
            _json.dump(data, f, ensure_ascii=False, indent=2)
        self.session_instruction_service._sticker_catalog_cache = ""
        self.logger.info(f"上传表情包: id={sid}, file={dest_name}, desc={description}")
        return Ok({"id": sid, "desc": description, "path": dest_name, "total": len(data)})

    @plugin_entry(
        id="register_sticker",
        name=tr("entries.register_sticker.name", default="注册表情包"),
        description=tr("entries.register_sticker.description", default="将一张图片注册为表情包，写入 sticker.json。需要提供图片文件的相对路径和描述。"),
        input_schema={"type": "object", "properties": {"image_path": {"type": "string", "description": "图片文件名，放在 data/sticker/ 目录下"}, "desc": {"type": "string", "description": "表情包描述，LLM 通过描述选择使用哪个表情包"}}, "required": ["image_path", "desc"], "additionalProperties": False},
    )
    async def register_sticker(self, image_path: str = "", desc: str = "", **_):
        """注册表情包到 sticker.json"""
        import os
        path = str(image_path or "").strip()
        description = str(desc or "").strip()
        if not path:
            return Err(SdkError("INVALID_INPUT: image_path 不能为空"))
        if not description:
            return Err(SdkError("INVALID_INPUT: desc 不能为空"))
        sticker_dir, sticker_json = self._sticker_paths()
        full_path = os.path.join(sticker_dir, path)
        if not os.path.exists(full_path):
            return Err(SdkError(f"NOT_FOUND: 图片文件不存在: data/sticker/{path}"))
        try:
            with open(sticker_json, "r", encoding="utf-8") as f:
                data = json.loads(f.read())
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        next_id = 1
        while str(next_id) in data:
            next_id += 1
        sid = str(next_id)
        data[sid] = {"desc": description, "path": path}
        os.makedirs(os.path.dirname(sticker_json), exist_ok=True)
        with open(sticker_json, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        self.session_instruction_service._sticker_catalog_cache = ""
        self.logger.info(f"注册表情包: id={sid}, path={path}, desc={description}")
        return Ok({"id": sid, "desc": description, "path": path, "total": len(data)})

    @plugin_entry(
        id="pick_directory",
        name=tr("entries.pick_directory.name", default="选择目录"),
        description=tr("entries.pick_directory.description", default="打开系统原生目录选择对话框，返回选中目录的绝对路径。"),
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    )
    async def pick_directory(self, **_):
        """打开系统原生目录选择器（后端路径——前端 NapCat 目录用 <input webkitdirectory> 原生选择，不走这里）。

        注意：tkinter 在插件子进程里可能无桌面会话/初始化失败，直接 Tk() 会导致
        进程崩溃（宿主收到 error=None）。因此这里捕获异常返回明确错误，绝不崩溃进程。
        """
        try:
            import tkinter as tk
            import tkinter.filedialog as fd
            root = tk.Tk()
            root.withdraw()
            root.attributes('-topmost', True)
            path = fd.askdirectory(title="选择 NapCat 安装目录")
            root.destroy()
            if path:
                return Ok({"path": str(path)})
            return Ok({"path": "", "cancelled": True})
        except Exception as e:
            self._emit_log("ERROR", f"pick_directory 打开目录选择失败: {type(e).__name__}: {e}")
            return Err(SdkError(f"PICK_DIRECTORY_FAILED: tkinter 目录选择在插件进程不可用: {type(e).__name__}"))

    @plugin_entry(
        id="get_napcat_webui",
        name=tr("entries.get_napcat_webui.name", default="获取 NapCat WebUI 地址"),
        description=tr("entries.get_napcat_webui.description", default="从 NapCat 日志提取 WebUI 登录地址和 token。"),
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    )
    async def get_napcat_webui(self, **_):
        url = self.napcat_service.get_webui_url()
        webui_lines = await self.napcat_service._read_napcat_webui_lines()
        return Ok({"url": url, "lines": webui_lines})

    @plugin_entry(id="get_buffer_state")
    async def get_buffer_state(self, **_):
        if not self.reply_buffer_service:
            return Ok({"pending": [], "count": 0})
        return Ok(self.reply_buffer_service.get_state())

    @plugin_entry(
        id="get_attention_state",
        name=tr("entries.get_attention_state.name", default="获取注意力状态"),
        description=tr("entries.get_attention_state.description", default="返回所有群聊的注意力分数和焦点状态。"),
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    )
    async def get_attention_state(self, **_):
        if not self.attention_service:
            return Ok({"enabled": False, "groups": [], "focus_group_id": "", "global_sleep": False})
        snapshot = self.attention_service.get_snapshot()
        return Ok({
            "enabled": snapshot.get("enabled", False),
            "focus_group_id": snapshot.get("focus_group_id", ""),
            "focus_score": snapshot.get("focus_score", 0.0),
            "global_sleep": self.attention_service.is_global_sleep(),
            "groups": snapshot.get("groups", []),
        })

    @plugin_entry(
        id="adjust_group_attention",
        name=tr("entries.adjust_group_attention.name", default="手动调整群注意力"),
        description=tr("entries.adjust_group_attention.description", default="给指定群手动增减注意力分数，正数为加分、负数为减分。"),
        input_schema={"type": "object", "properties": {
            "group_id": {"type": "string"},
            "delta": {"type": "number"},
        }, "required": ["group_id", "delta"], "additionalProperties": False},
    )
    async def adjust_group_attention(self, group_id: str, delta: float, **_):
        if not self.attention_service:
            return Err(SdkError("attention_service_not_initialized"))
        gid = str(group_id or "").strip()
        if not gid:
            return Err(SdkError("INVALID_INPUT: group_id 不能为空"))
        try:
            amount = float(delta or 0.0)
        except (TypeError, ValueError):
            return Err(SdkError("INVALID_INPUT: delta 必须是数字"))
        if amount > 0:
            await self.attention_service.boost_attention(gid, amount, reason="manual_adjust")
        elif amount < 0:
            await self.attention_service.consume_attention(gid, -amount, reason="manual_adjust")
        else:
            return Ok({"group_id": gid, "delta": 0.0, "note": "noop"})
        state = self.attention_service.get_state(gid)
        self._emit_log("INFO", f"[Attention] 手动调整 群{gid} delta={amount:+.1f} → score={state.attention_score:.1f}")
        return Ok({
            "group_id": gid,
            "delta": amount,
            "attention_score": float(state.attention_score),
        })

    @plugin_entry(
        id="ensure_napcat",
        name=tr("entries.ensure_napcat.name", default="启动 NapCat 进程"),
        description=tr("entries.ensure_napcat.description", default="启动 NapCat 外部进程并等待 OneBot 就绪（不连接 WebSocket）。"),
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    )
    async def ensure_napcat(self, **_):
        """仅启动 NapCat 进程，不连接"""
        await self._ensure_napcat_started()
        # 硬失败（目录缺失/启动器缺失/进程拉起失败）→ 明确报错，不返回
        # 「已启动」假象，也不让前端反复重试（ensure_napcat_started 已短路）。
        if self.napcat_service.has_hard_startup_error():
            return Err(SdkError(f"NAPCAT_START_FAILED: {self.napcat_service.get_startup_error()}"))
        ready = await self.napcat_service.wait_for_onebot_ready()
        if ready:
            await self._sync_napcat_qrcode_into_static()
            return Ok({"status": "napcat_ready"})
        return Ok({"status": "napcat_started", "onebot_ready": False})

    @plugin_entry(
        id="get_recent_logs",
        name=tr("entries.get_recent_logs.name", default="获取最近日志"),
        description=tr("entries.get_recent_logs.description", default="返回 QQ 插件文件日志的最近 N 行。"),
        input_schema={"type": "object", "properties": {"lines": {"type": "integer", "default": 100}}, "additionalProperties": False},
    )
    async def get_recent_logs(self, lines: int = 100, **_):
        """返回最近的日志行（内存缓冲区 + NapCat 输出）"""
        result_lines: list[str] = []
        buf = getattr(self, "_log_buffer", None)
        if buf and len(buf) > 0:
            n = max(1, min(int(lines or 100), self.LOG_BUFFER_SIZE))
            result_lines = list(buf)[-n:]
        # 追加 NapCat 输出
        try:
            napcat_lines = await self.napcat_service._read_napcat_webui_lines()
            if napcat_lines:
                result_lines.append("--- NapCat 输出 ---")
                result_lines.extend(napcat_lines)
        except Exception:
            pass
        if result_lines:
            return Ok({"lines": result_lines, "total": len(result_lines), "source": "memory+napcat"})
        # 回退：从日志文件读取
        import os
        log_path = ""
        try:
            handler = getattr(self, "file_logger", None)
            if handler and hasattr(handler, "handlers"):
                for h in handler.handlers:
                    if hasattr(h, "baseFilename"):
                        log_path = h.baseFilename
                        break
        except Exception:
            pass
        if log_path and os.path.exists(log_path):
            try:
                with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                    all_lines = f.readlines()
                n = max(1, min(int(lines or 100), 500))
                return Ok({"lines": [ln.rstrip("\n\r") for ln in all_lines[-n:]], "total": n, "source": "file"})
            except Exception as e:
                return Ok({"lines": [], "total": 0, "message": str(e)})
        return Ok({"lines": [], "total": 0, "message": "暂无日志（缓冲区为空且未找到日志文件）"})

    @plugin_entry(
        id="list_stickers",
        name=tr("entries.list_stickers.name", default="列出表情包"),
        description=tr("entries.list_stickers.description", default="读取 sticker.json 并返回所有已注册的表情包。"),
        input_schema={"type": "object", "properties": {}, "additionalProperties": False},
    )
    async def list_stickers(self, **_):
        """列出所有已注册表情包"""
        _sticker_dir, sticker_json = self._sticker_paths()
        try:
            with open(sticker_json, "r", encoding="utf-8") as f:
                data = json.loads(f.read())
        except Exception:
            data = {}
        if not isinstance(data, dict):
            data = {}
        items = []
        for sid, info in data.items():
            items.append({
                "id": sid,
                "desc": info.get("desc", "") if isinstance(info, dict) else str(info),
                "path": info.get("path", "") if isinstance(info, dict) else "",
            })
        return Ok({"stickers": items, "total": len(items)})

    @plugin_entry(
        id="sticker_lab",
        name=tr("entries.sticker_lab.name", default="表情包自测"),
        description=tr(
            "entries.sticker_lab.description",
            default="手动跑一次看图的整条链路：describe 只描述不落盘，save 收进表情包库，send 把库里某张发给指定 QQ，list 看库里有什么。用来确认识图和发表情包都通了。",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["describe", "save", "send", "list", "faces", "face_describe"],
                },
                "url": {"type": "string", "description": "图片 URL（describe / save 用）"},
                "note": {"type": "string", "description": "收藏时起的名字（save 用）"},
                "sticker_id": {"type": "string", "description": "要发的表情包 ID（send 用）"},
                "target_qq": {"type": "string", "description": "发给谁（send 用，私聊）"},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        metadata={"timeout": 60},
    )
    async def sticker_lab(
        self, action: str = "list", url: str = "", note: str = "",
        sticker_id: str = "", target_qq: str = "", **_,
    ):
        """表情包链路自测。四个动作各自独立，失败直接报错不吞。"""
        svc = getattr(self, "sticker_service", None)
        if svc is None:
            return Err(SdkError("NOT_READY: 表情包服务没起来"))
        act = str(action or "list").strip().lower()

        if act == "list":
            faces = getattr(self, "face_index", None)
            return Ok({
                "stickers": svc.list_all(limit=50),
                "stats": svc.stats(),
                "qq_faces": faces.list_all(limit=50) if faces else [],
                "qq_face_stats": faces.stats() if faces else {},
            })

        if act == "faces":
            faces = getattr(self, "face_index", None)
            if faces is None:
                return Err(SdkError("NOT_READY: 表情索引没起来"))
            await faces.start()
            lines, pending = faces.catalog_lines(limit=50)
            return Ok({
                "total": len(faces._faces),
                "pending": pending,
                "catalog": lines,
                "items": faces.list_all(limit=50),
                "stats": faces.stats(),
            })

        if act == "face_describe":
            faces = getattr(self, "face_index", None)
            if faces is None:
                return Err(SdkError("NOT_READY: 表情索引没起来"))
            await faces.start()
            done = await faces.describe_recent(limit=int(note or 2))
            return Ok({"described": done, "stats": faces.stats()})

        if act == "describe":
            target = str(url or "").strip()
            if not target:
                return Err(SdkError("INVALID_INPUT: url 不能为空"))
            vision = getattr(self, "vision_service", None)
            if vision is None:
                return Err(SdkError("NOT_READY: 识图服务没起来"))
            sticker_meta = await vision.describe_sticker(target)
            plain = await vision.describe(target)
            return Ok({
                "sticker_meta": sticker_meta,
                "plain": plain,
                "diag": vision.diag,
            })

        if act == "save":
            target = str(url or "").strip()
            if not target:
                return Err(SdkError("INVALID_INPUT: url 不能为空"))
            entry = await svc.save(target, origin="manual", note=str(note or ""))
            if not entry:
                return Err(SdkError("SAVE_FAILED: 没存进去（可能下载失败或识别为空）"))
            return Ok({"sticker": entry, "stats": svc.stats()})

        if act == "send":
            sid = str(sticker_id or "").strip()
            qq = str(target_qq or "").strip()
            if not sid or not qq:
                return Err(SdkError("INVALID_INPUT: sticker_id 和 target_qq 都要给"))
            faces = getattr(self, "face_index", None)
            if faces is not None and sid.lower().startswith("q"):
                face = faces.get(sid)
                if face is None:
                    return Err(SdkError(f"NOT_FOUND: 表情索引里没有 {sid}"))
                ok = await faces.send(face, user_id=qq)
                if not ok:
                    return Err(SdkError("SEND_FAILED: mface 和图片兜底都没成功，看日志"))
                return Ok({"sent": True, "kind": "qq_face", "face": face, "target_qq": qq})
            ok = await svc.send({"id": sid}, user_id=qq)
            if not ok:
                return Err(SdkError("SEND_FAILED: 没发出去，看日志"))
            return Ok({"sent": True, "kind": "file", "sticker_id": sid, "target_qq": qq})

        return Err(SdkError(f"INVALID_INPUT: 不认识的 action {act!r}"))

    @plugin_entry(
        id="gallery_lab",
        name=tr("entries.gallery_lab.name", default="图库自测"),
        description=tr(
            "entries.gallery_lab.description",
            default="手动跑图库：list 看有什么，stats 看统计，remember 收一张进来，import 把投喂目录里的图全收进来，describe 给没描述的补上，avatar 把某张设成 QQ 头像。",
        ),
        input_schema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": ["list", "stats", "remember", "describe", "avatar", "pick", "import"],
                },
                "url": {"type": "string", "description": "图片地址（remember 用，http 或 file:///）"},
                "query": {"type": "string", "description": "关键词（list / pick / avatar 用）"},
                "limit": {"type": "integer", "description": "看几张（list / describe 用）"},
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        metadata={"timeout": 120},
    )
    async def gallery_lab(
        self, action: str = "list", url: str = "", query: str = "",
        limit: int = 12, **_,
    ):
        """图库自测。每个动作独立，失败直接报错不吞。"""
        svc = getattr(self, "gallery_service", None)
        if svc is None:
            return Err(SdkError("NOT_READY: 图库服务没起来"))
        await svc.start()
        act = str(action or "list").strip().lower()

        if act == "stats":
            return Ok(svc.stats())

        if act == "list":
            rows = svc.list_items(query=str(query or ""), limit=int(limit or 12))
            for row in rows:
                row["avatar_url"] = ""  # 前端按需拼，这里只给元数据
            return Ok({
                "items": rows,
                "stats": svc.stats(),
                "catalog": svc.catalog_text(limit=min(int(limit or 12), 20)),
            })

        if act == "remember":
            target = str(url or "").strip()
            if not target:
                return Err(SdkError("INVALID_INPUT: url 不能为空"))
            item_id = await svc.remember(
                target, kind="image", desc="", source="manual",
            )
            if not item_id:
                return Err(SdkError("SAVE_FAILED: 没存进去（下载失败或写盘失败），看日志"))
            rows = svc.list_items(limit=1)
            return Ok({
                "id": item_id,
                "item": rows[0] if rows else {},
                "stats": svc.stats(),
            })

        if act == "describe":
            done = await svc.describe_missing(limit=int(limit or 6))
            return Ok({"described": done, "stats": svc.stats()})

        if act == "import":
            result = await svc.import_inbox(limit=int(limit or 40))
            result["stats"] = svc.stats()
            return Ok(result)

        if act == "avatar":
            key = str(query or "").strip()
            if not key:
                return Err(SdkError("INVALID_INPUT: query 要填编号（g1）或关键词"))
            return Ok(await svc.set_avatar(key))

        if act == "pick":
            found = svc.resolve(str(query or "").strip())
            if not found:
                return Err(SdkError("NOT_FOUND: 图库里没有对得上的"))
            item_id, item = found
            path = svc.path_of(item)
            return Ok({
                "id": item_id,
                "desc": item.get("desc") or "",
                "kind": item.get("kind") or "",
                "path": str(path) if path else "",
                "bytes": item.get("bytes") or 0,
            })

        return Err(SdkError(f"INVALID_INPUT: 不认识的 action {act!r}"))

    @ui.action(id="save_settings", label=tr("entries.save_settings.name", default="保存 QQ 自动回复设置"), refresh_context=True)
    @plugin_entry(id="save_settings", name=tr("entries.save_settings.name", default="保存 QQ 自动回复设置"), description=tr("entries.save_settings.description", default="保存 QQ 插件当前的 OneBot 地址、Token、NapCat 路径、回复概率、图库目录、主动搭话间隔、B 站刷视频开关和 backlog 标签等设置。"), input_schema={"type": "object", "properties": {"onebot_url": {"type": "string"}, "token": {"type": "string"}, "napcat_directory": {"type": "string"}, "show_napcat_window": {"type": "boolean"}, "reply_mode": {"type": "string", "enum": ["text", "voice", "both"]}, "show_onboarding": {"type": "boolean"}, "guide_step_napcat_done": {"type": "boolean"}, "guide_step_config_done": {"type": "boolean"}, "guide_step_runtime_done": {"type": "boolean"}, "normal_relay_probability": {"type": "number"}, "truth_reply_probability": {"type": "number"}, "backlog_labels": {"type": "array", "items": {"type": "object"}}, "strategy_mode": {"type": "string", "enum": ["neko_dynamic", "neko_scene"]}, "qq_connection_mode": {"type": "string", "enum": ["napcat", "napcat_forward", "open_platform"]}, "qq_open_app_id": {"type": "string"}, "qq_open_client_secret": {"type": "string"}, "qq_open_identity_probe_enabled": {"type": "boolean"}, "retroactive_review_max_messages": {"type": "integer"}, "retroactive_review_max_reply": {"type": "integer"}, "group_memory_enabled": {"type": "boolean"}, "group_member_memory_enabled": {"type": "boolean"}, "private_participant_memory_enabled": {"type": "boolean"}, "allow_cross_group_context": {"type": "boolean"}, "cross_domain_owner_memory": {"type": "boolean"}, "cross_domain_owner_context_rounds": {"type": "integer"}, "cross_domain_owner_longterm_memory": {"type": "boolean"}, "local_stt_url": {"type": "string"}, "gallery_dir": {"type": "string"}, "proactive_private_enabled": {"type": "boolean"}, "proactive_private_min_idle_minutes": {"type": "number"}, "proactive_private_min_gap_minutes": {"type": "number"}, "proactive_private_daily_limit": {"type": "number"}, "proactive_private_check_interval_seconds": {"type": "number"}, "scheduled_greeting_enabled": {"type": "boolean"}, "scheduled_greetings": {"type": "array", "items": {"type": "object", "properties": {"time": {"type": "string"}, "instruction": {"type": "string"}, "target": {"type": "string"}}, "required": ["time", "instruction"]}}, "scheduled_greeting_check_interval_seconds": {"type": "number"}, "bilibili_browse_enabled": {"type": "boolean"}, "bilibili_browse_interval_minutes": {"type": "integer"}}, "additionalProperties": False})
    async def save_settings(
        self,
        onebot_url: Optional[str] = None,
        token: Optional[str] = None,
        napcat_directory: Optional[str] = None,
        show_napcat_window: Optional[bool] = None,
        reply_mode: Optional[str] = None,
        show_onboarding: Optional[bool] = None,
        guide_step_napcat_done: Optional[bool] = None,
        guide_step_config_done: Optional[bool] = None,
        guide_step_runtime_done: Optional[bool] = None,
        normal_relay_probability: Optional[float] = None,
        truth_reply_probability: Optional[float] = None,
        backlog_labels: Optional[list[dict[str, Any]]] = None,
        group_attention_max_score: Optional[float] = None,
        group_attention_focus_threshold: Optional[float] = None,
        group_attention_focus_send_threshold: Optional[float] = None,
        group_attention_min_threshold: Optional[float] = None,
        group_attention_message_gain: Optional[float] = None,
        attention_base_rise_rate: Optional[float] = None,
        attention_message_boost: Optional[float] = None,
        attention_keyword_boost_ratio: Optional[float] = None,
        attention_honeymoon_seconds: Optional[int] = None,
        attention_fall_seconds: Optional[int] = None,
        attention_fall_rate: Optional[float] = None,
        attention_consume_ratio: Optional[float] = None,
        icebreaker_cold_threshold: Optional[int] = None,
        retroactive_review_max_messages: Optional[int] = None,
        retroactive_review_max_reply: Optional[int] = None,
        group_memory_enabled: Optional[bool] = None,
        group_member_memory_enabled: Optional[bool] = None,
        private_participant_memory_enabled: Optional[bool] = None,
        allow_cross_group_context: Optional[bool] = None,
        cross_domain_owner_memory: Optional[bool] = None,
        cross_domain_owner_context_rounds: Optional[int] = None,
        cross_domain_owner_longterm_memory: Optional[bool] = None,
        strategy_mode: Optional[str] = None,
        qq_connection_mode: Optional[str] = None,
        qq_open_app_id: Optional[str] = None,
        qq_open_client_secret: Optional[str] = None,
        qq_open_identity_probe_enabled: Optional[bool] = None,
        local_stt_url: Optional[str] = None,
        gallery_dir: Optional[str] = None,
        proactive_private_enabled: Optional[bool] = None,
        proactive_private_min_idle_minutes: Optional[float] = None,
        proactive_private_min_gap_minutes: Optional[float] = None,
        proactive_private_daily_limit: Optional[float] = None,
        proactive_private_check_interval_seconds: Optional[float] = None,
        scheduled_greeting_enabled: Optional[bool] = None,
        scheduled_greetings: Optional[list[dict[str, Any]]] = None,
        scheduled_greeting_check_interval_seconds: Optional[float] = None,
        bilibili_browse_enabled: Optional[bool] = None,
        bilibili_browse_interval_minutes: Optional[int] = None,
        **_,
    ):
        return await self.dashboard_service.save_settings(
            onebot_url=onebot_url,
            token=token,
            napcat_directory=napcat_directory,
            show_napcat_window=show_napcat_window,
            reply_mode=reply_mode,
            show_onboarding=show_onboarding,
            guide_step_napcat_done=guide_step_napcat_done,
            guide_step_config_done=guide_step_config_done,
            guide_step_runtime_done=guide_step_runtime_done,
            normal_relay_probability=normal_relay_probability,
            truth_reply_probability=truth_reply_probability,
            backlog_labels=backlog_labels,
            group_attention_max_score=group_attention_max_score,
            group_attention_focus_threshold=group_attention_focus_threshold,
            group_attention_focus_send_threshold=group_attention_focus_send_threshold,
            group_attention_min_threshold=group_attention_min_threshold,
            group_attention_message_gain=group_attention_message_gain,
            attention_base_rise_rate=attention_base_rise_rate,
            attention_message_boost=attention_message_boost,
            attention_keyword_boost_ratio=attention_keyword_boost_ratio,
            attention_honeymoon_seconds=attention_honeymoon_seconds,
            attention_fall_seconds=attention_fall_seconds,
            attention_fall_rate=attention_fall_rate,
            attention_consume_ratio=attention_consume_ratio,
            icebreaker_cold_threshold=icebreaker_cold_threshold,
            retroactive_review_max_messages=retroactive_review_max_messages,
            retroactive_review_max_reply=retroactive_review_max_reply,
            group_memory_enabled=group_memory_enabled,
            group_member_memory_enabled=group_member_memory_enabled,
            private_participant_memory_enabled=private_participant_memory_enabled,
            allow_cross_group_context=allow_cross_group_context,
            cross_domain_owner_memory=cross_domain_owner_memory,
            cross_domain_owner_context_rounds=cross_domain_owner_context_rounds,
            cross_domain_owner_longterm_memory=cross_domain_owner_longterm_memory,
            strategy_mode=strategy_mode,
            qq_connection_mode=qq_connection_mode,
            qq_open_app_id=qq_open_app_id,
            qq_open_client_secret=qq_open_client_secret,
            qq_open_identity_probe_enabled=qq_open_identity_probe_enabled,
            local_stt_url=local_stt_url,
            gallery_dir=gallery_dir,
            proactive_private_enabled=proactive_private_enabled,
            proactive_private_min_idle_minutes=proactive_private_min_idle_minutes,
            proactive_private_min_gap_minutes=proactive_private_min_gap_minutes,
            proactive_private_daily_limit=proactive_private_daily_limit,
            proactive_private_check_interval_seconds=proactive_private_check_interval_seconds,
            scheduled_greeting_enabled=scheduled_greeting_enabled,
            scheduled_greetings=scheduled_greetings,
            scheduled_greeting_check_interval_seconds=scheduled_greeting_check_interval_seconds,
            bilibili_browse_enabled=bilibili_browse_enabled,
            bilibili_browse_interval_minutes=bilibili_browse_interval_minutes,
        )

    @ui.action(id="add_trusted_user", label=tr("entries.add_trusted_user.name", default="添加信任用户"), refresh_context=True)
    @plugin_entry(id="add_trusted_user", name=tr("entries.add_trusted_user.name", default="添加信任用户"), description=tr("entries.add_trusted_user.description", default="把一个 QQ 号加入信任用户列表，并可设置权限、昵称和转发概率。"), input_schema={"type": "object", "properties": {"qq_number": {"type": "string"}, "level": {"type": "string", "default": "trusted"}, "nickname": {"type": "string", "default": ""}, "normal_relay_probability": {"type": "number"}}, "required": ["qq_number"]})
    async def add_trusted_user(self, qq_number: str, level: str = "trusted", nickname: str = "", normal_relay_probability: Optional[float] = None, **_):
        return await self.dashboard_service.add_trusted_user(
            qq_number=qq_number,
            level=level,
            nickname=nickname,
            normal_relay_probability=normal_relay_probability,
        )

    @ui.action(id="list_identity_claims", label=tr("entries.list_identity_claims.name", default="列出未认领的群内 ID"), refresh_context=False)
    @plugin_entry(id="list_identity_claims", name=tr("entries.list_identity_claims.name", default="列出未认领的群内 ID"), description=tr("entries.list_identity_claims.description", default="列出开放平台上出现过、但还不在信任用户名册里的群内 ID，以及可供人工合并的已有身份候选。"), input_schema={"type": "object", "properties": {}, "additionalProperties": False})
    async def list_identity_claims(self, **_):
        return await self.dashboard_service.list_identity_claims()

    @ui.action(id="bind_identity_account", label=tr("entries.bind_identity_account.name", default="合并到已有身份"), refresh_context=True)
    @plugin_entry(id="bind_identity_account", name=tr("entries.bind_identity_account.name", default="合并到已有身份"), description=tr("entries.bind_identity_account.description", default="把一个群内 ID 的信赖度账本并入已有身份。只能由人触发，系统不会自动合并任何身份。"), input_schema={"type": "object", "properties": {"user_id": {"type": "string"}, "target_user_id": {"type": "string"}}, "required": ["user_id", "target_user_id"], "additionalProperties": False})
    async def bind_identity_account(self, user_id: str, target_user_id: str, **_):
        return await self.dashboard_service.bind_identity_account(
            user_id=user_id, target_user_id=target_user_id,
        )

    @ui.action(id="unbind_identity_account", label=tr("entries.unbind_identity_account.name", default="撤销合并"), refresh_context=True)
    @plugin_entry(id="unbind_identity_account", name=tr("entries.unbind_identity_account.name", default="撤销合并"), description=tr("entries.unbind_identity_account.description", default="把一个群内 ID 从它被合并进的身份里拆回独立身份。误合并的唯一回滚方式。"), input_schema={"type": "object", "properties": {"user_id": {"type": "string"}}, "required": ["user_id"], "additionalProperties": False})
    async def unbind_identity_account(self, user_id: str, **_):
        return await self.dashboard_service.unbind_identity_account(
            user_id=user_id,
        )

    @ui.action(id="remove_trusted_user", label=tr("entries.remove_trusted_user.name", default="移除信任用户"), refresh_context=True)
    @plugin_entry(id="remove_trusted_user", name=tr("entries.remove_trusted_user.name", default="移除信任用户"), description=tr("entries.remove_trusted_user.description", default="把一个 QQ 号从信任用户列表中移除，不再按信任用户处理。"), input_schema={"type": "object", "properties": {"qq_number": {"type": "string"}}, "required": ["qq_number"]})
    async def remove_trusted_user(self, qq_number: str, **_):
        return await self.dashboard_service.remove_trusted_user(qq_number=qq_number)

    @ui.action(id="set_user_nickname", label=tr("entries.set_user_nickname.name", default="设置用户昵称"), refresh_context=True)
    @plugin_entry(id="set_user_nickname", name=tr("entries.set_user_nickname.name", default="设置用户昵称"), description=tr("entries.set_user_nickname.description", default="修改这个信任用户在回复里显示的昵称或称呼。"), input_schema={"type": "object", "properties": {"qq_number": {"type": "string"}, "nickname": {"type": "string", "default": ""}}, "required": ["qq_number"]})
    async def set_user_nickname(self, qq_number: str, nickname: str = "", **_):
        return await self.dashboard_service.set_user_nickname(qq_number=qq_number, nickname=nickname)

    @ui.action(id="add_trusted_group", label=tr("entries.add_trusted_group.name", default="添加信任群聊"), refresh_context=True)
    @plugin_entry(id="add_trusted_group", name=tr("entries.add_trusted_group.name", default="添加信任群聊"), description=tr("entries.add_trusted_group.description", default="把一个 QQ 群加入信任群聊列表，并可设置群等级和回复概率。"), input_schema={"type": "object", "properties": {"group_id": {"type": "string"}, "level": {"type": "string", "default": "normal"}, "normal_relay_probability": {"type": "number"}, "open_reply_probability": {"type": "number"}}, "required": ["group_id"]})
    async def add_trusted_group(self, group_id: str, level: str = "normal", normal_relay_probability: Optional[float] = None, open_reply_probability: Optional[float] = None, **_):
        return await self.dashboard_service.add_trusted_group(
            group_id=group_id,
            level=level,
            normal_relay_probability=normal_relay_probability,
            open_reply_probability=open_reply_probability,
        )

    @ui.action(id="remove_trusted_group", label=tr("entries.remove_trusted_group.name", default="移除信任群聊"), refresh_context=True)
    @plugin_entry(id="remove_trusted_group", name=tr("entries.remove_trusted_group.name", default="移除信任群聊"), description=tr("entries.remove_trusted_group.description", default="把一个 QQ 群从信任群聊列表中移除，不再按信任群聊处理。"), input_schema={"type": "object", "properties": {"group_id": {"type": "string"}}, "required": ["group_id"]})
    async def remove_trusted_group(self, group_id: str, **_):
        return await self.dashboard_service.remove_trusted_group(group_id=group_id)

    @plugin_entry(id="send_backlog_reply_direct", name=tr("entries.send_backlog_reply_direct.name", default="发送这条回复"), description=tr("entries.send_backlog_reply_direct.description", default="把你填写的内容直接回复到这条 QQ 消息，并在发送后把对应群聊标记为已处理。"), input_schema={"type": "object", "properties": {"source_type": {"type": "string"}, "target_id": {"type": "string"}, "sender_id": {"type": "string"}, "message_id": {"type": "string"}, "original_message": {"type": "string"}, "reply_text": {"type": "string"}}, "required": ["source_type", "target_id", "original_message", "reply_text"], "additionalProperties": False})
    async def send_backlog_reply_direct(self, source_type: str, target_id: str, original_message: str, reply_text: str, sender_id: str = "", message_id: str = "", **_):
        return await self.relay_service.send_backlog_reply_direct(
            source_type=source_type,
            target_id=target_id,
            original_message=original_message,
            reply_text=reply_text,
            sender_id=sender_id,
            message_id=message_id,
        )

    @plugin_entry(id="sync_qrcode", name=tr("entries.sync_qrcode.name", default="刷新二维码"), description=tr("entries.sync_qrcode.description", default="重新读取 NapCat 当前生成的 QQ 登录二维码，并更新到插件界面。"), input_schema={"type": "object", "properties": {}})
    async def sync_qrcode(self, **_):
        return await self.dashboard_service.sync_qrcode()

    @plugin_entry(id="start_auto_reply", name=tr("entries.start_auto_reply.name", default="启动自动回复"), description=tr("entries.start_auto_reply.description", default="开始监听 QQ 消息，并按当前配置自动回复或转发。"), input_schema={"type": "object", "properties": {}})
    async def start_auto_reply(self, **_):
        return await self.runtime_ops_service.start_auto_reply()

    @plugin_entry(id="stop_auto_reply", name=tr("entries.stop_auto_reply.name", default="停止自动回复"), description=tr("entries.stop_auto_reply.description", default="停止监听 QQ 消息，不再继续自动回复或转发。"), input_schema={"type": "object", "properties": {}})
    async def stop_auto_reply(self, **_):
        return await self.runtime_ops_service.stop_auto_reply()

    @plugin_entry(id="send_private_proactive_message", name=tr("entries.send_private_proactive_message.name", default="主动发送私聊消息"), description=tr("entries.send_private_proactive_message.description", default="根据你提供的内容生成一条新的 QQ 私聊消息，并直接发送给指定用户。"), input_schema={"type": "object", "properties": {"target": {"type": "string"}, "message": {"type": "string"}}, "required": ["target", "message"], "additionalProperties": False}, metadata={"timeout": 90})
    async def send_private_proactive_message(self, target: str, message: str, **_):
        return await self.proactive_message_service.send_private_message(target=target, message=message)

    @plugin_entry(id="send_group_proactive_message", name=tr("entries.send_group_proactive_message.name", default="主动发送群聊消息"), description=tr("entries.send_group_proactive_message.description", default="根据你提供的内容生成一条新的 QQ 群消息，并直接发送到指定群聊。"), input_schema={"type": "object", "properties": {"group_id": {"type": "string"}, "message": {"type": "string"}}, "required": ["group_id", "message"], "additionalProperties": False}, metadata={"timeout": 90})
    async def send_group_proactive_message(self, group_id: str, message: str, **_):
        return await self.proactive_message_service.send_group_message(group_id=group_id, message=message)

    @plugin_entry(id="trigger_proactive_private", name=tr("entries.trigger_proactive_private.name", default="触发私聊主动搭话"), description=tr("entries.trigger_proactive_private.description", default="立刻执行一次私聊主动搭话判定。force=true 时跳过静默时段、闲置时长、每日上限等全部限制，只用于调试。"), input_schema={"type": "object", "properties": {"target": {"type": "string"}, "force": {"type": "boolean"}}, "additionalProperties": False}, metadata={"timeout": 120})
    async def trigger_proactive_private(self, target: str = "", force: bool = False, **_):
        try:
            result = await self.proactive_private_service.try_proactive(target=target, force=force)
        except Exception as exc:
            self.logger.exception("主动私聊触发失败")
            return Err(SdkError(f"PROACTIVE_FAILED: {exc}"))
        if result.get("sent"):
            return Ok(result)
        return Ok({"status": "skipped", **result})

    @plugin_entry(id="get_proactive_private_state", name=tr("entries.get_proactive_private_state.name", default="查看主动搭话状态"), description=tr("entries.get_proactive_private_state.description", default="查看私聊主动搭话的开关、循环状态、各目标的上次互动时间与今日已发次数。"), input_schema={"type": "object", "properties": {}, "additionalProperties": False}, metadata={"timeout": 30})
    async def get_proactive_private_state(self, **_):
        return Ok(self.proactive_private_service.describe_state())

    # ── 断线补消息 ──────────────────────────────────────────

    @plugin_entry(id="get_catchup_state", name="查看断线补消息状态", description="查看每个会话处理到哪一条（message_seq）、上次补消息的结果和分项计数。断线后她收不到消息时先看这个。", input_schema={"type": "object", "properties": {}, "additionalProperties": False}, metadata={"timeout": 30})
    async def get_catchup_state(self, **_):
        return Ok(self.catchup_service.describe_state())

    @plugin_entry(id="trigger_catchup", name="手动补一次消息", description="立刻和 QQ 历史对账，把断线期间漏掉的消息补回来。force=true 时跳过 30 秒节流、把从没见过的会话也一起补。", input_schema={"type": "object", "properties": {"force": {"type": "boolean"}}, "additionalProperties": False}, metadata={"timeout": 120})
    async def trigger_catchup(self, force: bool = False, **_):
        try:
            result = await self.catchup_service.catch_up(reason="manual", force=force)
        except Exception as exc:
            self.logger.exception("手动补消息失败")
            return Err(SdkError(f"CATCHUP_FAILED: {exc}"))
        return Ok(result)

    # ── B 站见闻 ────────────────────────────────────────────

    @plugin_entry(id="bilibili_login_start", name="B站扫码登录", description="生成一个 B 站登录二维码（base64 PNG），用 B 站 App 扫码后调用 bilibili_login_check 完成登录。登录后才会抓得到字幕。", input_schema={"type": "object", "properties": {"force_new": {"type": "boolean", "description": "为 true 时丢弃旧二维码重新生成"}}, "additionalProperties": False}, metadata={"timeout": 90})
    async def bilibili_login_start(self, force_new: bool = False, **_):
        try:
            result = await self.bilibili_login_service.start(force_new=force_new)
        except Exception as exc:
            self.logger.exception("生成B站登录二维码失败")
            return Err(SdkError(f"BILI_LOGIN_START_FAILED: {exc}"))
        if not result.get("ok"):
            return Err(SdkError(f"BILI_LOGIN_START_FAILED: {result.get('reason')}"))
        return Ok(result)

    @plugin_entry(id="bilibili_login_check", name="查询B站扫码状态", description="查一次二维码扫描状态。返回 waiting/scanned/confirmed/done/timeout；done 时登录态已自动保存。", input_schema={"type": "object", "properties": {}, "additionalProperties": False}, metadata={"timeout": 90})
    async def bilibili_login_check(self, **_):
        try:
            return Ok(await self.bilibili_login_service.check())
        except Exception as exc:
            self.logger.exception("查询B站扫码状态失败")
            return Err(SdkError(f"BILI_LOGIN_CHECK_FAILED: {exc}"))

    @plugin_entry(id="bilibili_logout", name="退出B站登录", description="删掉本地保存的 B 站登录凭据。", input_schema={"type": "object", "properties": {}, "additionalProperties": False}, metadata={"timeout": 30})
    async def bilibili_logout(self, **_):
        removed = self.bilibili_login_service.clear_credential()
        try:
            await self.bilibili_browse_service.client.apply_credential({})
        except Exception:
            pass
        return Ok({"removed": removed, **self.bilibili_login_service.describe()})

    @plugin_entry(id="trigger_bilibili_browse", name="立刻刷一轮B站", description="马上执行一次 B 站刷视频，不等定时。返回这轮收集了多少条、深看了哪几条。", input_schema={"type": "object", "properties": {}, "additionalProperties": False}, metadata={"timeout": 300})
    async def trigger_bilibili_browse(self, **_):
        try:
            return Ok(await self.bilibili_browse_service.browse_once())
        except Exception as exc:
            self.logger.exception("手动刷B站失败")
            return Err(SdkError(f"BILI_BROWSE_FAILED: {exc}"))

    @plugin_entry(id="get_bilibili_state", name="查看B站见闻状态", description="查看刷视频开关、登录状态、见闻库条数、最近刷到的视频和下一轮时间。", input_schema={"type": "object", "properties": {}, "additionalProperties": False}, metadata={"timeout": 30})
    async def get_bilibili_state(self, **_):
        payload = self.bilibili_browse_service.describe_state()
        payload["login"] = self.bilibili_login_service.describe()
        return Ok(payload)

    async def _stop_auto_reply_runtime(self, *, stop_napcat: bool):
        await self.runtime_ops_service.stop_runtime(stop_napcat=stop_napcat)

    def _track_handler_task(self, task: asyncio.Task) -> None:
        self.handler_runtime_service.track_handler_task(task)

    def _on_handler_task_done(self, task: asyncio.Task) -> None:
        self.handler_runtime_service.on_handler_task_done(task)

    async def _record_backlog_message(self, message: Dict[str, Any]) -> None:
        await self.backlog_service.record_message(message)

    @plugin_entry(id="get_backlog_summary", name=tr("entries.get_backlog_summary.name", default="读取待审阅摘要"), description=tr("entries.get_backlog_summary.description", default="查看当前哪些群还有待处理消息，以及每个群的大致积压情况。"), input_schema={"type": "object", "properties": {}})
    async def get_backlog_summary(self, **_):
        return Ok(await self.backlog_service.get_summary_payload())

    @plugin_entry(id="get_group_backlog_detail", name=tr("entries.get_group_backlog_detail.name", default="读取群聊待审阅详情"), description=tr("entries.get_group_backlog_detail.description", default="查看这个群当前每条待处理消息的详细内容，方便逐条回复或处理。"), input_schema={"type": "object", "properties": {"group_id": {"type": "string"}}, "required": ["group_id"]})
    async def get_group_backlog_detail(self, group_id: str, **_):
        normalized_group_id = self._validate_group_id(group_id)
        return Ok(await self.backlog_service.get_group_detail_payload(normalized_group_id))

    @plugin_entry(id="mark_group_backlog_reviewed", name=tr("entries.mark_group_backlog_reviewed.name", default="标记群聊已处理"), description=tr("entries.mark_group_backlog_reviewed.description", default="把这个群当前所有待处理消息标记为已处理，不再继续显示为未审阅。"), input_schema={"type": "object", "properties": {"group_id": {"type": "string"}}, "required": ["group_id"]})
    async def mark_group_backlog_reviewed(self, group_id: str, **_):
        normalized_group_id = self._validate_group_id(group_id)
        return Ok(await self.backlog_service.mark_group_reviewed_payload(normalized_group_id))

    @plugin_entry(
        id="forget_group_memory",
        name=tr("entries.forget_group_memory.name", default="清除群记忆"),
        description=tr("entries.forget_group_memory.description", default="删除指定群聊的全部长期记忆（facts/reflections/persona）。幂等，重试安全。"),
        input_schema={"type": "object", "properties": {"group_id": {"type": "string"}}, "required": ["group_id"]},
    )
    async def forget_group_memory(self, group_id: str, **_):
        normalized_group_id = self._validate_group_id(group_id)
        from utils.config_manager import get_config_manager
        try:
            _, her_name, _, _, _, _, _, _, _ = get_config_manager().get_character_data()
        except Exception:
            her_name = "neko"
        try:
            result = await self.memory_bridge.post_scoped_forget(
                her_name,
                subject=self.memory_bridge.group_subject(normalized_group_id),
            )
            self._emit_log("INFO", f"群 {normalized_group_id} 记忆已清除: {result}")
            return Ok({"group_id": normalized_group_id, "forgotten": True, "detail": result})
        except Exception as exc:
            self._emit_log("ERROR", f"清除群 {normalized_group_id} 记忆失败: {exc}")
            return Err(SdkError(f"FORGET_FAILED: {exc}"))

    @plugin_entry(
        id="get_user_profiles",
        name=tr("entries.get_user_profiles.name", default="获取用户画像"),
        description=tr("entries.get_user_profiles.description", default="读取当前缓存的用户画像列表，包含身份信息和从长期记忆中提取的事实摘要。"),
        input_schema={"type": "object", "properties": {}},
    )
    async def get_user_profiles(self, **_):
        profiles: list[dict[str, Any]] = []
        now = time.time()
        cache = getattr(self.session_instruction_service, "_user_profile_cache", {}) or {}
        perm_mgr = self.permission_mgr

        # 收集所有有缓存画像的用户（cache key 格式: sender_id:scope_key）
        seen: set[str] = set()
        for cache_key, (text, expire_at) in list(cache.items()):
            # 从复合 key 中提取 sender_id
            sender_id = str(cache_key).split(":", 1)[0] if ":" in str(cache_key) else str(cache_key)
            if sender_id in seen:
                continue
            seen.add(sender_id)
            nickname = ""
            level = "none"
            if perm_mgr:
                nickname = perm_mgr.get_nickname(sender_id) or ""
                level = perm_mgr.get_permission_level(sender_id)
            profiles.append({
                "sender_id": sender_id,
                "nickname": nickname,
                "permission_level": level,
                "profile_text": text,
                "cached": True,
                "expires_in_seconds": max(0, int(expire_at - now)),
            })

        # 也列出信任用户中还没有画像的
        if perm_mgr:
            for u in perm_mgr.list_users():
                sid = str(u.get("qq") or "").strip()
                if sid and sid not in seen:
                    seen.add(sid)
                    profiles.append({
                        "sender_id": sid,
                        "nickname": str(u.get("nickname") or ""),
                        "permission_level": str(u.get("level") or "normal"),
                        "profile_text": "",
                        "cached": False,
                        "expires_in_seconds": 0,
                    })


        return Ok({"profiles": profiles, "count": len(profiles), "cached_count": sum(1 for p in profiles if p["cached"])})

    # ==========================================
    # 提示词编辑器
    # ==========================================

    @plugin_entry(
        id="get_prompt_editor_state",
        name=tr("entries.get_prompt_editor_state.name", default="获取提示词编辑器状态"),
        description=tr("entries.get_prompt_editor_state.description", default="返回当前语言下的各层提示词元数据和配置，供提示词编辑器使用。"),
        input_schema={"type": "object", "properties": {"mode": {"type": "string"}}, "additionalProperties": False},
    )
    async def get_prompt_editor_state(self, mode: str = "", locale: str = "", **_):
        frontend_mode = str(mode or "").strip()
        stored_mode = str((self._qq_settings or {}).get("qq_connection_mode", "napcat") or "napcat").strip()
        mode = frontend_mode if frontend_mode in ("napcat", "napcat_forward", "open_platform") else stored_mode
        from utils.language_utils import get_global_language_full
        frontend_locale = str(locale or "").strip()
        # #2500 第 2 步：兜底也用全码。这个 locale 有两个身份——查 i18n bundle 的
        # 键，以及回传给前端、被 save_prompt_override 原样当作覆盖的存储键。短码
        # 'zh' 会让繁中用户在编辑器里看到（并覆盖）简体那份。
        # ⚠️ 必须和 session_instruction_service._resolve_static_layer 的兜底同时
        # 翻：写侧用 'zh-TW' 存、读侧还按 'zh' 的候选链找，覆盖会静默失效。
        locale = frontend_locale if frontend_locale else get_global_language_full()
        strategy_mode = getattr(self, "_strategy_mode", "neko_dynamic")
        is_napcat = mode in ("napcat", "napcat_forward")
        overrides = (self._qq_settings or {}).get("prompt_overrides") or {}
        if not isinstance(overrides, dict):
            overrides = {}
        layers = []
        for layer_def in self.session_instruction_service._PROMPT_LAYERS:
            lid = layer_def["id"]
            is_runtime = layer_def.get("runtime", False)
            is_scene = lid.startswith("scene_") or lid.startswith("naming_")
            # 按连接模式过滤 format/scene 层
            if lid.startswith("format_"):
                if is_napcat:
                    if lid == "format_open_platform":
                        continue
                    if lid == "format_neko_dynamic" and strategy_mode != "neko_dynamic":
                        continue
                    if lid == "format_neko_scene" and strategy_mode != "neko_scene":
                        continue
                else:
                    # 开放平台只显示 format_open_platform
                    if lid != "format_open_platform":
                        continue
            # NapCat 按策略模式过滤 scene 层
            if is_scene and strategy_mode == "neko_dynamic":
                if lid not in ("scene_group_dynamic",):
                    continue
            # 开放平台跳过 scene/naming 层
            if not is_napcat and is_scene:
                continue
            # 获取当前生效的文本
            i18n_key = layer_def.get("i18n_key", "")
            default_text = ""
            if not is_runtime:
                from .prompt_fragment_templates import (
                    ATTENTION_PROMPT_SECTION,
                    CHARACTER_PROMPT_SECTION,
                    DETAIL_CONSTRAINTS_SECTION,
                    FORMAT_PROMPT_SECTION,
                    FORMAT_PROMPT_SECTION_NEKO_DYNAMIC,
                    FORMAT_PROMPT_SECTION_OPEN_PLATFORM,
                    OUTPUT_PROMPT_SECTION,
                    ROLE_PROMPT_SECTION,
                    TIME_PROMPT_SECTION,
                )
                from .scene_prompt_templates import (
                    SCENE_COLLECTIVE_GROUP,
                    SCENE_DIRECTED_GROUP,
                    SCENE_KIRA_UNIFIED_GROUP,
                    SCENE_PRIVATE_CHAT,
                    SCENE_SHARED_GROUP,
                )
                default_map = {
                    "role_prompt_section": ROLE_PROMPT_SECTION,
                    "attention_prompt_section": ATTENTION_PROMPT_SECTION,
                    "character_prompt_section": CHARACTER_PROMPT_SECTION,
                    "time_prompt_section": TIME_PROMPT_SECTION,
                    "detail_constraints_section": DETAIL_CONSTRAINTS_SECTION,
                    "output_prompt_section": OUTPUT_PROMPT_SECTION,
                    "format_prompt_section": FORMAT_PROMPT_SECTION,
                    "format_prompt_section_neko_dynamic": FORMAT_PROMPT_SECTION_NEKO_DYNAMIC,
                    "format_prompt_section_open_platform": FORMAT_PROMPT_SECTION_OPEN_PLATFORM,
                    "prompts.group.collective": SCENE_COLLECTIVE_GROUP,
                    "prompts.group.directed": SCENE_DIRECTED_GROUP,
                    "prompts.group.kira_unified": SCENE_KIRA_UNIFIED_GROUP,
                    "prompts.group.shared_session": SCENE_SHARED_GROUP,
                    "prompts.private.body": SCENE_PRIVATE_CHAT,
                }
                default_text = default_map.get(i18n_key, "")
            has_override = False
            effective_text = ""
            if not is_runtime:
                # ⚠️ 走候选链而不是精确匹配 ``overrides[locale]``：覆盖桶的键是
                # 存的时候那次的 locale，未必等于现在解析出来的（#2500 之前繁中
                # 用户的兜底是短码 'zh'）。运行时按候选链读，编辑器精确匹配的
                # 话，那份覆盖照样生效、编辑器却报「未修改」。
                found = resolve_prompt_override(overrides, locale, i18n_key)
                if found is not None:
                    has_override = True
                    effective_text = str(found[1] or "")
                else:
                    effective_text = self.i18n.t(i18n_key, locale=locale, default=default_text)
            if lid == "time" and self.fatigue_service:
                effective_text = self.fatigue_service.get_dynamic_time_context()
            layers.append({
                "id": lid,
                "i18n_key": i18n_key,
                "is_runtime": is_runtime,
                "required_placeholders": layer_def.get("required_placeholders", []),
                "format_after": layer_def.get("format_after", False),
                "has_override": has_override,
                "default_text": default_text,
                "effective_text": effective_text,
            })
        self._emit_log("INFO", f"[PromptEditor] mode={mode} is_napcat={is_napcat} strategy={strategy_mode} locale={locale} layers={len(layers)}")
        self.logger.info(f"[PromptEditor] mode={mode} is_napcat={is_napcat} strategy={strategy_mode} locale={locale} layers={len(layers)}")
        proactive_topics = list((self._qq_settings or {}).get("proactive_topics") or [])
        if not proactive_topics and self.attention_gate_service:
            proactive_topics = list(getattr(self.attention_gate_service, "_DEFAULT_PROACTIVE_TOPICS", []))
        return Ok({
            "mode": mode,
            "locale": locale,
            "strategy_mode": strategy_mode,
            "layers": layers,
            "proactive_topics": proactive_topics,
        })

    @plugin_entry(
        id="save_prompt_override",
        name=tr("entries.save_prompt_override.name", default="保存提示词覆盖"),
        description=tr("entries.save_prompt_override.description", default="保存某个提示词层的自定义覆盖值到 business_config。"),
        input_schema={
            "type": "object",
            "properties": {
                "locale": {"type": "string"},
                "layer_id": {"type": "string"},
                "text": {"type": "string", "maxLength": 65536},
            },
            "required": ["locale", "layer_id", "text"],
            "additionalProperties": False,
        },
    )
    async def save_prompt_override(self, locale: str, layer_id: str, text: str, **_):
        locale = str(locale or "").strip()
        layer_id = str(layer_id or "").strip()
        text_val = str(text or "")
        if not locale:
            return Err(SdkError("INVALID_INPUT: locale 不能为空"))
        if not layer_id:
            return Err(SdkError("INVALID_INPUT: layer_id 不能为空"))
        # 验证 layer_id 存在且非 runtime
        layer_def = next((ld for ld in self.session_instruction_service._PROMPT_LAYERS if ld["id"] == layer_id), None)
        if layer_def is None:
            return Err(SdkError(f"INVALID_INPUT: 未知的提示词层: {layer_id}"))
        if layer_def.get("runtime"):
            return Err(SdkError(f"INVALID_INPUT: 运行时层不可编辑: {layer_id}"))
        def _save_override(settings):
            raw_overrides = settings.get("prompt_overrides") or {}
            overrides = (
                dict(raw_overrides) if isinstance(raw_overrides, dict) else {}
            )
            overrides.setdefault(locale, {})
            overrides[locale] = dict(overrides[locale])
            overrides[locale][layer_def["i18n_key"]] = (
                text_val if text_val.strip() else ""
            )
            settings["prompt_overrides"] = overrides
            return True

        success = await QQAutoReplyPlugin._mutate_business_config(
            self, _save_override,
        )
        if success:
            self.session_instruction_service._discard_all_sessions_for_prompt_change()
        return Ok({"persisted": success, "layer_id": layer_id, "locale": locale})

    @plugin_entry(
        id="reset_prompt_override",
        name=tr("entries.reset_prompt_override.name", default="重置提示词覆盖"),
        description=tr("entries.reset_prompt_override.description", default="删除某个提示词层的自定义覆盖值，恢复默认。"),
        input_schema={
            "type": "object",
            "properties": {"locale": {"type": "string"}, "layer_id": {"type": "string"}},
            "required": ["locale", "layer_id"],
            "additionalProperties": False,
        },
    )
    async def reset_prompt_override(self, locale: str, layer_id: str, **_):
        locale = str(locale or "").strip()
        layer_id = str(layer_id or "").strip()
        if not locale or not layer_id:
            return Err(SdkError("INVALID_INPUT"))
        layer_def = next((ld for ld in self.session_instruction_service._PROMPT_LAYERS if ld["id"] == layer_id), None)
        if layer_def is None or layer_def.get("runtime"):
            return Err(SdkError(f"INVALID_INPUT: 无法重置的层: {layer_id}"))
        override_found = False

        def _reset_override(settings):
            nonlocal override_found
            raw_overrides = settings.get("prompt_overrides") or {}
            overrides = {
                bucket: (dict(entries) if isinstance(entries, dict) else entries)
                for bucket, entries in (
                    raw_overrides.items() if isinstance(raw_overrides, dict) else ()
                )
            }
            i18n_key = layer_def["i18n_key"]
            removed = False

            # 先删精确桶。它可能存着空串（编辑器清空时的存法），resolve 看不见
            # 那种，光靠下面的循环会漏。
            exact = overrides.get(locale)
            if isinstance(exact, dict) and i18n_key in exact:
                exact.pop(i18n_key)
                removed = True

            # 再一直删到「解析不出覆盖」为止。只删精确桶是不够的：候选链上
            # 还有别的桶（存量短码 'zh'，以及每条链都会带上的 'zh-CN' /
            # 'en'），删掉 zh-TW 之后它们会顶上来 —— 「恢复默认」就变成了
            # 「换一份旧覆盖」，而且再按一次还是它。
            # ⚠️ 代价说清楚：同一个人如果按 locale 分别调过这一层的提示词，
            # 重置会把该层其它 locale 的那几份一起清掉。单用户桌面应用里，
            # 这比「按了恢复默认却恢复不掉」轻 —— 后者没有任何出路。
            while True:
                found = resolve_prompt_override(overrides, locale, i18n_key)
                if found is None:
                    break
                overrides[found[0]].pop(i18n_key, None)
                removed = True

            if not removed:
                return False
            override_found = True
            for bucket in [
                b for b, entries in overrides.items()
                if isinstance(entries, dict) and not entries
            ]:
                overrides.pop(bucket, None)
            settings["prompt_overrides"] = overrides
            return True

        success = await QQAutoReplyPlugin._mutate_business_config(
            self, _reset_override,
        )
        if override_found:
            if success:
                self.session_instruction_service._discard_all_sessions_for_prompt_change()
            return Ok({"persisted": success, "layer_id": layer_id, "locale": locale})
        return Ok({"persisted": True, "layer_id": layer_id, "locale": locale, "reason": "no_override_found"})

    @plugin_entry(id="save_group_prompt")
    async def save_group_prompt(self, group_id: str, text: str, **_):
        """保存某个群的专属提示词。text 为空字符串则视为删除。"""
        gid = str(group_id or "").strip()
        if not gid:
            return Err(SdkError("INVALID_GROUP_ID: group_id 不能为空"))
        custom_text = str(text or "").strip()
        def _save_group_prompt(settings):
            group_prompts = dict(settings.get("group_prompts") or {})
            if custom_text:
                group_prompts[gid] = custom_text
            else:
                group_prompts.pop(gid, None)
            settings["group_prompts"] = group_prompts
            return True

        success = await QQAutoReplyPlugin._mutate_business_config(
            self, _save_group_prompt,
        )
        if success:
            if custom_text:
                self._emit_log(
                    "INFO",
                    f"已保存群 {gid} 的自定义提示词 ({len(custom_text)} 字符)",
                )
            else:
                self._emit_log("INFO", f"已清除群 {gid} 的自定义提示词")
        else:
            self._emit_log(
                "WARNING",
                f"群 {gid} 自定义提示词写盘失败，运行时变更未持久化",
            )
        # 清除该群的当前会话，下次回复时重新注入新提示词
        if self.session_runtime_service:
            discarded = await self._run_with_session_lock(
                f"group:{gid}",
                lambda: self.session_runtime_service.discard_session(f"group:{gid}", reason="group_prompt_changed"),
            )
            if discarded is False:
                self._emit_log("WARNING", f"群 {gid} 会话因记忆结算失败暂未重置，新提示词将在下次会话重建时生效")
        return Ok({"persisted": success, "group_id": gid, "has_text": bool(custom_text)})

    @plugin_entry(id="delete_group_prompt")
    async def delete_group_prompt(self, group_id: str, **_):
        """删除某个群的专属提示词。"""
        gid = str(group_id or "").strip()
        if not gid:
            return Err(SdkError("INVALID_GROUP_ID: group_id 不能为空"))
        existed = False

        def _delete_group_prompt(settings):
            nonlocal existed
            group_prompts = dict(settings.get("group_prompts") or {})
            existed = gid in group_prompts
            if not existed:
                return False
            group_prompts.pop(gid)
            settings["group_prompts"] = group_prompts
            return True

        success = await QQAutoReplyPlugin._mutate_business_config(
            self, _delete_group_prompt,
        )
        if existed:
            if self.session_runtime_service:
                discarded = await self._run_with_session_lock(
                    f"group:{gid}",
                    lambda: self.session_runtime_service.discard_session(f"group:{gid}", reason="group_prompt_deleted"),
                )
                if discarded is False:
                    self._emit_log("WARNING", f"群 {gid} 会话因记忆结算失败暂未重置，新提示词将在下次会话重建时生效")
            if success:
                self._emit_log("INFO", f"已删除群 {gid} 的自定义提示词")
            else:
                self._emit_log(
                    "WARNING",
                    f"群 {gid} 自定义提示词删除写盘失败，运行时变更未持久化",
                )
            return Ok({"persisted": success, "group_id": gid, "deleted": True})
        return Ok({"persisted": True, "group_id": gid, "deleted": False, "reason": "not_found"})

    @plugin_entry(id="get_group_prompts")
    async def get_group_prompts(self, **_):
        """获取所有群的专属提示词映射。"""
        group_prompts = dict(self._qq_settings.get("group_prompts") or {})
        return Ok({"group_prompts": group_prompts})

    async def _maybe_notify_backlog_summary(self, *, group_id: str) -> None:
        await self.backlog_service.maybe_notify_summary(group_id=group_id)

    async def _process_messages(self):
        await self.message_dispatcher.process_messages()

    async def _handle_message(self, message: Dict[str, Any]):
        await self.message_dispatcher.handle_message(message)

    async def _handle_private_message(self, sender_id: str, message_text: str, attachments: Optional[list[Dict[str, Any]]] = None, user_nickname: Optional[str] = None):
        # 互动时间打点在 message_dispatcher.handle_private_message 里统一做，
        # 那里才是所有私聊（含缓冲重投递）的必经之处。
        await self.message_dispatcher.handle_private_message(sender_id, message_text, attachments=attachments, user_nickname=user_nickname)

    async def _handle_group_message(self, group_id: str, sender_id: str, message_text: str, is_at_bot: bool, attachments: Optional[list[Dict[str, Any]]] = None, user_nickname: Optional[str] = None):
        await self.message_dispatcher.handle_group_message(group_id, sender_id, message_text, is_at_bot, attachments=attachments, user_nickname=user_nickname)

    @staticmethod
    @staticmethod
    def _sanitize_message_text(text: str, *, is_reply_to_bot: bool = False) -> str:
        import re
        # 回复标签 → 人类可读格式
        if is_reply_to_bot:
            text = re.sub(r"\[CQ:reply,id=\d+[^\]]*\]", "[回复你的消息]", text)
        else:
            text = re.sub(r"\[CQ:reply,id=\d+[^\]]*\]", "[回复他人的消息]", text)
        text = re.sub(r"\[CQ:at,qq=all\]", "@全体成员", text)
        text = re.sub(r"\[CQ:at,qq=(\d+)\]", r"@用户\1", text)
        return text

    async def _handle_normal_relay(self, message_text: str, sender_id: str, source_type: str, source_id: str, relay_probability: Optional[float] = None):
        return await self.relay_service.handle_normal_relay(
            message_text,
            sender_id,
            source_type,
            source_id,
            relay_probability=relay_probability,
        )

    @plugin_entry(
        id="save_proactive_topics",
        name=tr("entries.save_proactive_topics.name", default="保存主动发言话题"),
        description=tr("entries.save_proactive_topics.description", default="保存主动发言话题列表，每行一个话题。"),
        input_schema={"type": "object", "properties": {"topics": {"type": "array", "items": {"type": "string"}}}, "required": ["topics"], "additionalProperties": False},
    )
    async def save_proactive_topics(self, topics: list[str] = None, **_):
        topic_list = [str(t).strip() for t in (topics or []) if str(t).strip()]
        self._qq_settings["proactive_topics"] = topic_list
        success = await self._persist_business_config()
        self._emit_log("INFO", f"主动发言话题已更新: {len(topic_list)}条")
        return Ok({"count": len(topic_list), "persisted": success})

    async def _run_message_handler(self, message: Dict[str, Any]) -> None:
        await self.handler_runtime_service.run_message_handler(message)
