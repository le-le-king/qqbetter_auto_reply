"""消息链模型 — 对齐 KiraAI 的 MessageChain 递归结构。

每个消息元素都有 .repr 属性用于注入 LLM prompt，
MessageChain 可以嵌套（reply.chain, forward.chains）。"""

from __future__ import annotations

import json as _json
import pathlib
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

# ── 基础元素 ──────────────────────────────────────────────

class MessageElement(ABC):
    """消息元素基类。"""

    @property
    @abstractmethod
    def repr(self) -> str:
        """LLM prompt 中的文本表示。"""
        ...


class Text(MessageElement):
    def __init__(self, text: str) -> None:
        self.text: str = str(text)

    @property
    def repr(self) -> str:
        return self.text


class Image(MessageElement):
    def __init__(self, url: str = "", file: str = "") -> None:
        self.url: str = str(url or "")
        self.file: str = str(file or "")

    @property
    def repr(self) -> str:
        return "[图片]"


class At(MessageElement):
    def __init__(self, pid: str, nickname: str = "") -> None:
        self.pid: str = str(pid)
        self.nickname: str = str(nickname or "")

    @property
    def repr(self) -> str:
        if self.pid == "all":
            return "[@全体成员]"
        if self.nickname:
            return f"[@{self.nickname}]"
        return f"[@用户{self.pid}]"


class Reply(MessageElement):
    def __init__(self, message_id: str, chain: Optional[MessageChain] = None) -> None:
        self.message_id: str = str(message_id)
        self.chain: Optional[MessageChain] = chain  # 被引用消息的内容链

    @property
    def repr(self) -> str:
        if self.chain:
            return self.chain.repr
        return f"[引用 {self.message_id}]"


class Forward(MessageElement):
    def __init__(self, chains: list[MessageChain]) -> None:
        self.chains: list[MessageChain] = chains

    @property
    def repr(self) -> str:
        return "[转发]"


class Emoji(MessageElement):
    def __init__(self, emoji_id: str, name: str = "") -> None:
        self.emoji_id: str = str(emoji_id)
        self.name: str = str(name or "")

    @property
    def repr(self) -> str:
        label = self.name or _face_name(self.emoji_id)
        if label:
            return f"[表情 {label}]"
        return f"[表情 {self.emoji_id}]"


class Sticker(MessageElement):
    """表情包（QQ 的 ``mface``：商城表情 / 收藏的梗图）。

    和 ``Emoji`` 不是一回事：Emoji 是那条小黄脸，只有编号；Sticker 是一张
    真图，能下载、能收藏、能再发出去。``desc`` 是 VLM 看过之后的一句话，
    模型靠它才知道对方甩的是不是"无语"。
    """

    def __init__(self, url: str = "", summary: str = "", desc: str = "",
                 emoji_id: str = "") -> None:
        self.url: str = str(url or "")
        self.summary: str = str(summary or "")
        self.desc: str = str(desc or "")
        self.emoji_id: str = str(emoji_id or "")

    @property
    def repr(self) -> str:
        if self.desc:
            return f"[表情包 {self.desc}]"
        if self.summary and self.summary.strip("[]") != "动画表情":
            return f"[表情包 {self.summary}]"
        return "[表情包]"


#: QQ 小黄脸编号 → 中文名。emoji.json 就在插件根目录，跟本文件同级。
#: 编号直接甩给模型等于什么都没说（`[表情 178]`），而 `[表情 微笑]` 她能接住。
_FACE_NAMES: Optional[dict[str, str]] = None


def _face_name(emoji_id: str) -> str:
    global _FACE_NAMES
    if _FACE_NAMES is None:
        try:
            _FACE_NAMES = _json.loads(
                (pathlib.Path(__file__).parent / "emoji.json").read_text(encoding="utf-8")
            )
        except Exception:
            _FACE_NAMES = {}
    return str(_FACE_NAMES.get(str(emoji_id), "") or "")


class Record(MessageElement):
    def __init__(self, bs64: str = "", file_id: str = "") -> None:
        self.bs64: str = str(bs64 or "")
        self.file_id: str = str(file_id or "")

    @property
    def repr(self) -> str:
        return "[语音]"


class Notice(MessageElement):
    def __init__(self, text: str) -> None:
        self.text: str = str(text)

    @property
    def repr(self) -> str:
        return self.text


class Poke(MessageElement):
    def __init__(self, pid: str) -> None:
        self.pid: str = str(pid)

    @property
    def repr(self) -> str:
        return "[戳一戳]"


class File(MessageElement):
    def __init__(self, name: str = "", bs64: str = "", url: str = "") -> None:
        self.name: str = str(name or "")
        self.bs64: str = str(bs64 or "")
        # 可选直链 URL（Lagrange 类后端 data.url / NapCat get_*_file_url 换取），
        # 纯增量：repr / bs64 行为不变。
        self.url: str = str(url or "")

    @property
    def repr(self) -> str:
        return f"[文件 {self.name}]" if self.name else "[文件]"


class JsonCard(MessageElement):
    """QQ JSON 卡片消息（小程序/分享卡片等）。"""
    def __init__(self, raw_json: str) -> None:
        self.raw: str = str(raw_json or "")
        self.title: str = ""
        self.desc: str = ""
        self.app: str = ""
        self.nick: str = ""
        self.prompt: str = ""
        self.url: str = ""
        self._parse()

    def _parse(self) -> None:
        try:
            card = _json.loads(self.raw)
        except Exception:
            return
        if not isinstance(card, dict):
            return
        meta = card.get("meta") if isinstance(card.get("meta"), dict) else {}
        detail = meta.get("detail_1") if isinstance(meta.get("detail_1"), dict) else {}
        news = meta.get("news") if isinstance(meta.get("news"), dict) else {}
        self.title = str(detail.get("title") or news.get("title") or "")
        self.desc = str(detail.get("desc") or news.get("desc") or "")
        self.app = str(card.get("app") or "")
        host = detail.get("host")
        self.nick = (
            str(host.get("nick") or "") if isinstance(host, dict) else ""
        )
        self.prompt = str(card.get("prompt") or "")
        # 分享卡片里的真实地址分散在不同字段：小程序卡片在 qqdocurl，
        # 新版分享在 jumpUrl，资讯类在 news.jumpUrl。原来这几个字段一个都
        # 没读，导致"发一个 bilibili 视频卡片过来"在 prompt 里只剩一行
        # "[卡片 标题: 描述]"，模型既看不到链接也打不开。
        for candidate in (
            detail.get("qqdocurl"),
            detail.get("jumpUrl"),
            detail.get("url"),
            news.get("jumpUrl"),
            news.get("url"),
        ):
            text = str(candidate or "").strip()
            if text.startswith(("http://", "https://")):
                self.url = text
                break

    @property
    def repr(self) -> str:
        parts: list[str] = ["[卡片"]
        if self.title:
            parts.append(f" {self.title}")
        if self.desc:
            parts.append(f": {self.desc}")
        if self.nick:
            parts.append(f" (来自{self.nick})")
        parts.append("]")
        if self.url:
            # 单独一行放链接：模型扫描时更容易把 URL 当整体抓走，不会连着
            # 前面的描述一起抄错。
            parts.append(f"\n{self.url}")
        return "".join(parts)


class XmlCard(MessageElement):
    """QQ XML 卡片消息（旧版分享、音乐、位置等）。

    真实地址藏在 ``<msg url="...">`` 属性里，卡片可见文字往往只有
    "分享了一个链接"。不把 url 捞出来，模型完全不知道对方发的是哪个
    页面，也就没法去打开它——这正是"她打不开我发的链接"的直接原因之一。
    """

    _URL_ATTR_RE = re.compile(r"""url\s*=\s*["']([^"']+)["']""")
    _TITLE_RE = re.compile(
        r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", re.S,
    )

    def __init__(self, raw_xml: str) -> None:
        self.raw: str = str(raw_xml or "")
        self.url: str = ""
        self.title: str = ""
        self._parse()

    def _parse(self) -> None:
        match = self._URL_ATTR_RE.search(self.raw)
        if match:
            candidate = match.group(1).strip()
            if candidate.startswith(("http://", "https://")):
                self.url = candidate
        title_match = self._TITLE_RE.search(self.raw)
        if title_match:
            self.title = title_match.group(1).strip()[:80]

    @property
    def repr(self) -> str:
        if self.url and self.title:
            return f"[链接卡片 {self.title}]\n{self.url}"
        if self.url:
            return f"[链接卡片]\n{self.url}"
        if self.title:
            return f"[卡片 {self.title}]"
        return "[卡片]"


# ── 消息链 ────────────────────────────────────────────────

@dataclass
class MessageChain:
    """有序消息元素列表，支持嵌套（reply/forward）。"""
    elements: list[MessageElement] = field(default_factory=list)
    sender_name: str = ""
    sender_id: str = ""
    timestamp: int = 0
    message_id: str = ""

    def add(self, element: MessageElement) -> "MessageChain":
        self.elements.append(element)
        return self

    @property
    def repr(self) -> str:
        """拼接所有元素的 repr，用于注入 LLM 上下文。"""
        return "".join(e.repr for e in self.elements)

    @property
    def plain_text(self) -> str:
        """提取纯文本（仅 Text 元素）。"""
        return "".join(e.text for e in self.elements if isinstance(e, Text))

    @staticmethod
    def empty() -> "MessageChain":
        return MessageChain()


# ── 构建工具 ──────────────────────────────────────────────

def chain_from_onebot_message(msg: dict[str, Any]) -> MessageChain:
    """从 OneBot 消息 dict 构建消息链（不含嵌套展开）。"""
    chain = MessageChain(
        sender_name=(msg.get("sender") or {}).get("nickname") or str(msg.get("user_id") or ""),
        sender_id=str(msg.get("user_id") or ""),
        timestamp=int(msg.get("time") or 0),
        message_id=str(msg.get("message_id") or ""),
    )
    segments = msg.get("message") or []
    if not isinstance(segments, list):
        return chain
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        t = seg.get("type") or ""
        data = seg.get("data") or {}
        if t == "text":
            chain.add(Text(str(data.get("text") or "")))
        elif t == "image":
            chain.add(Image(
                url=str(data.get("url") or ""),
                file=str(data.get("file") or ""),
            ))
        elif t == "at":
            chain.add(At(pid=str(data.get("qq") or "")))
        elif t == "reply":
            chain.add(Reply(message_id=str(data.get("id") or "")))
        elif t == "face":
            chain.add(Emoji(emoji_id=str(data.get("id") or "")))
        elif t == "record":
            chain.add(Record(file_id=str(data.get("file") or "")))
        elif t == "forward":
            chain.add(Forward(chains=[]))
        elif t == "video":
            chain.add(Text("[视频]"))
        elif t == "json":
            chain.add(JsonCard(raw_json=str(data.get("data") or "")))
        elif t == "xml":
            chain.add(XmlCard(raw_xml=str(data.get("data") or "")))
        elif t == "file":
            chain.add(File(name=str(data.get("file") or "")))
    return chain
