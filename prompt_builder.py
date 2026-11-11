from __future__ import annotations

import re
from typing import Any, Optional

#: 消息里出现这些就认为对方发的是 B 站视频。和 bilibili_video 里的正则
#: 保持一致，但这里只用来"提醒模型去调工具"，不需要精确到能拿到号。
_BILI_LINK_RE = re.compile(
    r"bilibili\.com|b23\.tv|(?<![0-9A-Za-z])BV[0-9A-Za-z]{10}",
    re.IGNORECASE,
)


class QQPromptBuilder:
    def __init__(self, plugin: Any):
        self.plugin = plugin

    def build_user_title(
        self,
        *,
        permission_level: str,
        sender_id: str,
        master_name: str,
        custom_nickname: str | None,
        user_nickname: str | None,
        is_group: bool,
    ) -> str:
        if is_group:
            if custom_nickname:
                return custom_nickname
            if user_nickname:
                return user_nickname
            return self.plugin.i18n.t("prompts.default_qq_user", default="QQ用户{sender_id}", sender_id=sender_id)
        if permission_level == "admin":
            return master_name if master_name else self.plugin.i18n.t("prompts.default_master", default="主人")
        if custom_nickname:
            return custom_nickname
        if user_nickname:
            return user_nickname
        return self.plugin.i18n.t("prompts.default_qq_user", default="QQ用户{sender_id}", sender_id=sender_id)

    def build_character_card_fields(self, current_character: dict[str, Any]) -> dict[str, Any]:
        character_card_fields: dict[str, Any] = {}
        for key, value in current_character.items():
            if key not in [
                "_reserved", "voice_id", "system_prompt", "model_type",
                "live2d", "vrm", "vrm_animation", "lighting", "vrm_rotation",
                "live2d_item_id", "item_id", "idleAnimation",
            ]:
                if isinstance(value, (str, int, float, bool)) and value:
                    character_card_fields[key] = value
        return character_card_fields

    def should_use_memory_context(self, *, is_group: bool, permission_level: str, requested: Optional[bool]) -> bool:
        if requested is None:
            if is_group:
                # 群路径的 None 默认跟配置走：normal 群路径由 dispatcher 显式
                # 传值，但回溯审核（_reply_to_ignored_message）、rapid-fire
                # flush 等旁路构造请求时不带该字段。None→False 会让这些回复
                # 既不召回 scoped 记忆也不落成员轮，还会把共享群会话的
                # memory_enabled 翻回 False、阻断 idle flush 结算。让 None
                # 恒等于"配置的群记忆策略"，任何旁路自动对齐，不再逐点补。
                settings = getattr(self.plugin, "_qq_settings", {}) or {}
                return bool(settings.get("group_memory_enabled", False))
            if permission_level == "admin":
                return True
            # 非 admin 私聊：跟 participant 记忆开关走（对偶群路径的
            # None→配置策略）。开着时该轮以对方的 participant 域读写，
            # legacy 私聊主人语料仍然只属于 admin。
            settings = getattr(self.plugin, "_qq_settings", {}) or {}
            return bool(settings.get("private_participant_memory_enabled", False))
        return bool(requested)

    def should_persist_memory(self, *, should_use_memory_context: bool, requested: Optional[bool], is_group: bool = False) -> bool:
        if requested is None:
            if is_group:
                # 群会话的持久化跟配置策略走、与"本轮是否召回"解耦：
                # proactive 等路径显式关本轮召回（use=False）不代表要把
                # 共享会话的 memory_enabled 翻回 False——那会搁浅已缓冲
                # 的 opt-in 历史直到下一条普通消息。
                settings = getattr(self.plugin, "_qq_settings", {}) or {}
                return bool(settings.get("group_memory_enabled", False))
            return should_use_memory_context
        return bool(requested)

    def build_prompt_message(
        self,
        *,
        is_group: bool,
        group_facing: bool,
        group_scene_mode: str,
        user_title: str,
        sender_id: str,
        group_id: str | None,
        message: str,
        current_message_id: str = "",
        is_reply_to_bot: bool = False,
        quoted_message_id: str = "",
        mentions_other_user: bool = False,
        mentions_all: bool = False,
    ) -> str:
        if is_group and not group_facing:
            return self._with_bilibili_hint(
                self.plugin._build_group_turn_message(
                    group_scene_mode=group_scene_mode,
                    user_title=user_title,
                    sender_id=sender_id,
                    group_id=group_id,
                    message=message,
                    current_message_id=current_message_id,
                    is_reply_to_bot=is_reply_to_bot,
                    quoted_message_id=quoted_message_id,
                    mentions_other_user=mentions_other_user,
                    mentions_all=mentions_all,
                )
            )
        return self._with_bilibili_hint(message)

    def _with_bilibili_hint(self, message: str) -> str:
        """对方发来 B 站链接时，在消息末尾压一句硬提醒。

        为什么不能只靠工具描述：工具描述是"建议"，模型完全可以不调——
        而对方发链接过来，最差的表现就是她凭标题瞎聊，甚至编一段没发生
        的内容。这句话把"先打开再回"变成约束。

        成本是一次正则匹配，比"每条消息都预取视频内容"（多 4~8 秒）便宜
        得多。如果实测发现模型照样不调，再考虑在 reply_context_node 里
        预取。
        """
        if not _BILI_LINK_RE.search(message or ""):
            return message
        return (
            message
            + "\n\n（对方消息里有 B 站链接。回话前先用 bilibili_watch 打开，"
            "看清楚它到底讲了什么，再结合里面的弹幕和字幕说；"
            "不要凭标题猜，也不要说自己看了却讲不出具体内容。）"
        )
