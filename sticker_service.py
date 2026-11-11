"""表情包库 —— 收藏对方发的梗图、按感觉翻出来、甩出去。

**复用插件原有的存储**，不另起一套：
``data/sticker/`` 放图，``data/sticker.json`` 是索引（``{"1": {"desc":..., "path":...}}``）。
理由是插件 UI 已经有一个"表情包"页面在读这两个东西，系统提示词里的
``<sticker>ID</sticker>`` 目录也从这里生成。另建一套的结果是同一个概念在
两个地方各有一半，用户会看到"我明明存了，列表里没有"。

在这个格式上只多加了几个可选字段（``emotion`` / ``added_at`` / ``last_sent_at``），
老条目没有也能照常读——加载时全部给默认值。

``desc`` 是这套东西的检索键：模型看到的是提示词里的一行行描述，它要挑哪个
ID 全靠这句话。所以收藏一张图时 VLM 生成的不是"画面描述"，而是
"画面 + 情绪 + 什么场合用"三合一，塞进 desc 里。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Optional

# 单张上限。QQ 表情包正常 20~500KB，超过 6MB 的多半是对方发错的大图。
_MAX_BYTES = 6 * 1024 * 1024
# desc 长度上限。这句话会进系统提示词，一涨就是几百字上下文；但太短
# 会把"什么场合用"那一截切掉，模型就没法判断该不该甩这张。
_MAX_DESC = 60

_EXT_BY_MAGIC = (
    (b"GIF8", ".gif"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"\x89PNG", ".png"),
    (b"RIFF", ".webp"),
    (b"BM", ".bmp"),
)


class QQStickerService:
    """表情包库。所有对外方法都不抛异常，返回 None/False 表示没成。"""

    DIRNAME = "sticker"
    INDEX_FILENAME = "sticker.json"

    def __init__(self, plugin: Any, vision: Any = None, faces: Any = None):
        self.plugin = plugin
        self.vision = vision
        # QQ 表情标识索引。有它的时候，收藏/发送都优先走它——不下载、不上传，
        # 每张图从"一次模型调用 + 一个文件"降到"几十字节的指针"。
        self.faces = faces
        self._entries: dict[str, dict[str, Any]] = {}
        self._loaded = False
        self._lock = asyncio.Lock()
        self.diag = {
            "saved": 0,
            "dup": 0,
            "download_fail": 0,
            "describe_empty": 0,
            "send_ok": 0,
            "send_fail": 0,
            "not_found": 0,
        }

    # ── 路径 ────────────────────────────────────────────────

    @property
    def _root(self) -> Path:
        try:
            base = Path(self.plugin.data_path())
        except Exception:
            base = Path(".")
        return base

    @property
    def _index_path(self) -> Path:
        return self._root / self.INDEX_FILENAME

    @property
    def _dir(self) -> Path:
        return self._root / self.DIRNAME

    # ── 载入 / 落盘 ─────────────────────────────────────────

    async def start(self) -> None:
        await self._load()

    async def stop(self) -> None:
        # 没有攒着写的缓冲：每次存完立刻落盘。一张表情包不值几毫秒。
        return

    async def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        await asyncio.to_thread(self._load_sync)

    def _load_sync(self) -> None:
        try:
            payload = json.loads(self._index_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception as exc:
            # 索引坏了不能让她整库消失：留一份坏的给人看，然后从空库继续。
            self.plugin.logger.warning(f"表情包索引读取失败: {exc}")
            try:
                self._index_path.rename(self._index_path.with_suffix(".json.broken"))
            except Exception:
                pass
            return
        if not isinstance(payload, dict):
            return
        for sid, info in payload.items():
            if isinstance(info, dict):
                entry = dict(info)
            else:
                # 老格式可能是 {"1": "一只猫"}，当成 desc。
                entry = {"desc": str(info or ""), "path": ""}
            entry["id"] = str(sid)
            self._entries[str(sid)] = entry
        if self._entries:
            self.plugin.logger.info(f"[Sticker] 已载入 {len(self._entries)} 张表情包")

    async def _flush(self) -> None:
        payload = {
            sid: {
                "desc": str(entry.get("desc") or ""),
                "path": str(entry.get("path") or ""),
                **{
                    k: entry[k]
                    for k in ("emotion", "added_at", "sent", "last_sent_at")
                    if k in entry
                },
            }
            for sid, entry in self._entries.items()
        }
        try:
            await asyncio.to_thread(self._write_index, payload)
        except Exception:
            self.plugin.logger.debug("表情包索引写入失败", exc_info=True)
        # 系统提示词里的 <sticker> 目录是缓存的，改完必须让它重读，
        # 否则她看得见旧列表：存了新图却挑不到。
        instruction = getattr(self.plugin, "session_instruction_service", None)
        if instruction is not None:
            try:
                instruction._sticker_catalog_cache = ""
            except Exception:
                pass

    def _write_index(self, payload: dict[str, Any]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        tmp = self._index_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._index_path)

    # ── 存 ──────────────────────────────────────────────────

    async def save(
        self,
        locator: str,
        *,
        origin: str = "",
        note: str = "",
        recognize: bool = True,
    ) -> Optional[dict[str, Any]]:
        """把一张图收进表情包库。返回条目；下载失败/重复/太大返回 None。

        ``note`` 是她自己给这张图起的名字。她自己说的比 VLM 描述的准，
        所以有 note 时放在 desc 前面。
        """
        url = str(locator or "").strip()
        if not url or self.vision is None:
            return None
        await self._load()

        raw = await self.vision.fetch_bytes(url)
        if not raw:
            self.diag["download_fail"] += 1
            return None
        if len(raw) > _MAX_BYTES:
            self.diag["download_fail"] += 1
            self.plugin.logger.info(f"[Sticker] 图片太大跳过: {len(raw)} bytes")
            return None

        digest = hashlib.md5(raw).hexdigest()[:12]
        async with self._lock:
            # 内容去重：同一张图（哪怕换了个人转发）只存一份。
            for sid, entry in list(self._entries.items()):
                if digest in str(entry.get("path") or ""):
                    self.diag["dup"] += 1
                    return await self._merge_note(sid, note)

            meta: dict[str, str] = {}
            if recognize:
                try:
                    meta = await self.vision.describe_sticker(url) or {}
                except Exception:
                    meta = {}

            desc = self._compose_desc(note=note, meta=meta)
            if not desc:
                self.diag["describe_empty"] += 1
                self.plugin.logger.info("[Sticker] 识别为空且无备注，不入库")
                return None

            ext = self._guess_ext(raw)
            filename = f"sticker_{digest}{ext}"
            try:
                await asyncio.to_thread(self._write_blob, filename, raw)
            except Exception:
                self.diag["download_fail"] += 1
                self.plugin.logger.debug("表情包写入失败", exc_info=True)
                return None

            sid = self._next_id()
            entry = {
                "desc": desc,
                "path": filename,
                "emotion": str(meta.get("emotion") or "").strip()[:16],
                "added_at": time.time(),
                "sent": 0,
                "last_sent_at": 0.0,
                "origin": str(origin or "").strip(),
            }
            self._entries[sid] = entry
        await self._flush()
        self.diag["saved"] += 1
        self.plugin.logger.info(f"[Sticker] 收藏第 {len(self._entries)} 张: {desc[:30]}")
        return {"id": sid, **entry}

    async def _merge_note(self, sid: str, note: str) -> dict[str, Any]:
        """已经在库里了：只补一句备注，不覆盖原来的描述。"""
        entry = self._entries.get(sid) or {}
        text = str(note or "").strip()
        if text and text not in str(entry.get("desc") or ""):
            entry["desc"] = f"{text} {str(entry.get('desc') or '')}".strip()[:_MAX_DESC]
            self._entries[sid] = entry
            await self._flush()
        return {"id": sid, **entry}

    @staticmethod
    def _compose_desc(*, note: str, meta: dict[str, str]) -> str:
        """把 note、画面、情绪拼成一句能被检索的话。

        语气上要像"她自己记的"，不是说明书：``翻白眼的猫 / 无语 / 敷衍他``。
        """
        # 顺序有讲究：她自己的叫法 → 情绪 → 画面 → 用法。
        # 情绪放前面是因为它是检索的高频词（"无语""嘲讽"），而画面描述
        # 往往一长串、容易把后面的用法挤出上限。
        parts: list[str] = []
        clean_note = str(note or "").strip()
        if clean_note:
            parts.append(clean_note)
        emotion = str(meta.get("emotion") or "").strip()
        if emotion:
            parts.append(emotion)
        scene = str(meta.get("scene") or "").strip()
        if scene:
            parts.append(scene)
        usage = str(meta.get("usage") or "").strip()
        if usage and usage not in scene:
            parts.append(usage)
        return " / ".join(parts)[:_MAX_DESC]

    def _write_blob(self, filename: str, raw: bytes) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        (self._dir / filename).write_bytes(raw)

    def _next_id(self) -> str:
        """沿用插件原有的数字 ID：提示词里 ``<sticker>3</sticker>`` 靠它。"""
        biggest = 0
        for sid in self._entries:
            try:
                biggest = max(biggest, int(sid))
            except (TypeError, ValueError):
                continue
        return str(biggest + 1)

    @staticmethod
    def _guess_ext(raw: bytes) -> str:
        for magic, ext in _EXT_BY_MAGIC:
            if raw.startswith(magic):
                return ext
        return ".jpg"

    # ── 找 ──────────────────────────────────────────────────

    def search(self, query: str = "", *, limit: int = 3) -> list[dict[str, Any]]:
        """按她那句话找表情包。空 query = 挑最久没发过的。"""
        entries = list(self._entries.values())
        if not entries:
            self.diag["not_found"] += 1
            return []
        text = str(query or "").strip()
        if not text:
            ordered = sorted(entries, key=lambda e: float(e.get("last_sent_at") or 0))
            return [dict(e) for e in ordered[:limit]]

        scored: list[tuple[float, dict[str, Any]]] = []
        for entry in entries:
            score = self._score(text, entry)
            if score > 0:
                scored.append((score, entry))
        if not scored:
            self.diag["not_found"] += 1
            return []
        # 同分时优先很久没发过的，免得老是同一张。
        scored.sort(
            key=lambda pair: (pair[0], -float(pair[1].get("last_sent_at") or 0)),
            reverse=True,
        )
        return [dict(e) for _score, e in scored[:limit]]

    @staticmethod
    def _score(query: str, entry: dict[str, Any]) -> float:
        """desc 整句命中 > 情绪命中 > 2-gram 局部命中。"""
        desc = str(entry.get("desc") or "").lower()
        emotion = str(entry.get("emotion") or "").lower()
        q = str(query or "").lower().strip()
        if not q or not desc:
            return 0.0
        if q in desc:
            return 6.0
        if emotion and q in emotion:
            return 4.0
        hits = sum(1 for gram in _bigrams(q) if gram in desc)
        return min(hits, 4) * 0.8

    def list_all(self, *, limit: int = 10) -> list[dict[str, Any]]:
        ordered = sorted(
            self._entries.values(),
            key=lambda e: float(e.get("added_at") or 0),
            reverse=True,
        )
        return [dict(e) for e in ordered[:limit]]

    def resolve_path(self, sid: str) -> Optional[Path]:
        entry = self._entries.get(str(sid or "").strip())
        if not entry:
            return None
        filename = str(entry.get("path") or "").strip()
        if not filename:
            return None
        path = self._dir / filename
        return path if path.is_file() else None

    def stats(self) -> dict[str, Any]:
        return {"total": len(self._entries), **self.diag}

    # ── 发 ──────────────────────────────────────────────────

    async def send(
        self,
        sticker: dict[str, Any],
        *,
        user_id: str = "",
        group_id: str = "",
    ) -> bool:
        """把一张表情包甩出去。成功返回 True。

        走 ``send_private_image`` / ``send_group_image``，和插件自己的
        ``<sticker>`` 投递同一条路。``sub_type="1"`` 让 QQ 把它当贴纸显示
        而不是一张"图片"——表情包发成普通图片会带白框。
        """
        await self._load()
        sid = str(sticker.get("id") or "").strip()
        path = self.resolve_path(sid)
        if path is None:
            self.diag["send_fail"] += 1
            self.plugin.logger.warning(f"[Sticker] 表情包文件不存在: id={sid}")
            return False

        client = getattr(self.plugin, "qq_client", None)
        if client is None:
            self.diag["send_fail"] += 1
            return False

        locator = str(path)
        message_id = None
        try:
            if group_id:
                message_id = await client.send_group_image(
                    group_id, locator, sub_type="1",
                )
            elif user_id:
                message_id = await client.send_private_image(user_id, locator)
        except Exception as exc:
            self.plugin.logger.warning(
                f"[Sticker] 表情包发送异常: {type(exc).__name__}: {exc}"
            )
            message_id = None

        if not message_id:
            self.diag["send_fail"] += 1
            return False

        self.diag["send_ok"] += 1
        entry = self._entries.get(sid)
        if entry is not None:
            entry["sent"] = int(entry.get("sent") or 0) + 1
            entry["last_sent_at"] = time.time()
            await self._flush()
        return True

    # ── 工具 ────────────────────────────────────────────────

    def build_save_definition(self):
        from main_logic.tool_calling import ToolDefinition

        return ToolDefinition(
            name=SAVE_TOOL_NAME,
            description=(
                "收藏表情包。对方发了个表情包或好玩的图，你想留着以后自己甩的时候用。"
                "默认收下最近收到的那一张；连发了好几张可以一次收多张 1-3 张。"
                "收的时候顺手给它起个名字（note），以后你才能按'那个无语的猫'找回来。"
                "表情包只记标识不存文件，收多少都不占地方。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "note": {
                        "type": "string",
                        "description": "你给它起的名字或记住它的理由，比如'翻白眼的猫''这条用来敷衍他'",
                    },
                    "count": {
                        "type": "integer",
                        "description": "收最近几张（1-3，默认 1）",
                    },
                },
                "required": [],
            },
            metadata={"source": "qqbetter_auto_reply"},
        )

    def build_send_definition(self):
        from main_logic.tool_calling import ToolDefinition

        return ToolDefinition(
            name=SEND_TOOL_NAME,
            description=(
                "发表情包。聊天时想甩一张图代替说话就用它，图会立刻发出去，"
                "发完接着用文字说你的话。按感觉找：'无语''在笑''嘲讽''摆烂'。"
                "一轮最多发一张。库里没有合适的就别硬发，也别自己编 ID 写 <sticker> 标签。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "你想要什么感觉的图，比如'无语'、'狂笑'、'鄙视'",
                    },
                },
                "required": ["query"],
            },
            metadata={"source": "qqbetter_auto_reply"},
        )

    async def execute_save(
        self,
        arguments: Any,
        *,
        sender_id: str = "",
        group_id: str = "",
    ) -> str:
        """收藏最近收到的表情包 / 图片。

        表情包只记标识（QQ 服务器就是存储），普通图片才落盘——图片 URL 有时效，
        不落盘过两天就变成一张打不开的链接。
        """
        args = arguments if isinstance(arguments, dict) else {}
        note = str(args.get("note") or "").strip()
        try:
            count = int(args.get("count") or 1)
        except (TypeError, ValueError):
            count = 1
        count = max(1, min(count, 3))

        client = getattr(self.plugin, "qq_client", None)
        if client is None:
            return "（现在连不上 QQ，没收成。）"
        items = client.recent_media(limit=count, sender_id=sender_id)
        if not items and sender_id:
            # 群里"这张存下来"经常指的不是当前发言者发的那张（A 发图、B 说
            # 这图好玩）。按人找不到就退到"最近不管谁发的"，比直接回
            # "没东西可收"合理。
            items = client.recent_media(limit=count)
        if not items:
            return "（最近没收到图，没东西可收。）"

        saved: list[str] = []
        for item in items:
            if item.get("kind") == "sticker" and self.faces is not None:
                label = await self._save_face(item, note=note)
                if label:
                    saved.append(label)
                continue
            entry = await self.save(
                str(item.get("locator") or ""),
                origin=str(item.get("sender_id") or ""),
                note=note,
            )
            if entry:
                saved.append(str(entry.get("desc") or "")[:24])
        if not saved:
            return "（这张没存进去，可能刚才已经收过了。）"
        total = len(self._entries) + (len(self.faces._faces) if self.faces is not None else 0)
        self.plugin.logger.info(f"[Sticker] 工具收藏 {len(saved)} 张")
        return f"收好了，现在手上能用的表情包 {total} 张。这张是：{'；'.join(saved)}"

    async def _save_face(self, item: dict[str, Any], *, note: str) -> str:
        """把 QQ 表情包收进索引：存标识 + 认一次描述（唯一花钱的一步）。"""
        segment = item.get("segment")
        if not isinstance(segment, dict):
            return ""
        entry = self.faces.remember(
            segment,
            sender_id=str(item.get("sender_id") or ""),
            group_id=str(item.get("group_id") or ""),
        )
        if not entry:
            return ""
        desc = await self.faces.ensure_desc(entry)
        if note:
            desc = self.faces.add_note(entry, note) or desc
        if not desc:
            return ""
        return desc[:24]


    async def execute_send(
        self,
        arguments: Any,
        *,
        sender_id: str = "",
        group_id: str = "",
        already_sent: bool = False,
    ) -> str:
        """按感觉找一张发出去。先翻 QQ 表情（零成本），再翻本地图库。"""
        await self._load()
        args = arguments if isinstance(arguments, dict) else {}
        query = str(args.get("query") or "").strip()
        if already_sent:
            return "（这一轮已经发过一张了，别再甩，用说的。）"

        if self.faces is not None:
            face_matches = self.faces.search(query, limit=3)
            if face_matches:
                ok = await self.faces.send(
                    face_matches[0], user_id=sender_id, group_id=group_id,
                )
                if ok:
                    self.plugin.logger.info(f"[Sticker] 工具发出 QQ 表情: query={query!r}")
                    picked = self.faces.label(face_matches[0])[:24]
                    return f"发出去了。这张是：{picked}。接着用文字说你的话，别解释这张图。"

        if not self._entries:
            if self.faces is not None and self.faces._faces:
                return (
                    "（没有对得上'{}'的。手上那些我还没看清是什么，"
                    "先别发，用文字接话。）".format(query or "这张")
                )
            return "（你的表情包库还是空的，先收几张再发。）"

        matches = self.search(query, limit=3)
        if not matches:
            sample = self._sample_labels(limit=6)
            return (
                f"（库里没有'{query}'的图。现在手上有：{sample or '还没收几张'}。"
                "要么换成手上的，要么这次就别发。）"
            )
        ok = await self.send(matches[0], user_id=sender_id, group_id=group_id)
        if not ok:
            return "（图没发出去。别说你发了，用文字接话。）"
        self.plugin.logger.info(f"[Sticker] 工具发出: query={query!r}")
        picked = str(matches[0].get("desc") or "")[:24]
        return f"发出去了。这张是：{picked}。接着用文字说你的话，别解释这张图。"

    def _sample_labels(self, *, limit: int = 6) -> str:
        labels: list[str] = []
        if self.faces is not None:
            for entry in self.faces.list_all(limit=limit * 2, only_described=True):
                label = self.faces.label(entry)
                if label and label not in labels:
                    labels.append(label[:12])
                if len(labels) >= limit:
                    break
        for entry in self.list_all(limit=limit * 2):
            label = str(entry.get("desc") or "").strip()
            if label and label not in labels:
                labels.append(label[:12])
            if len(labels) >= limit:
                break
        return "、".join(labels)


#: 工具名。散落在 reply_generation_service 里做硬编码比较，所以做成常量。
SAVE_TOOL_NAME = "save_sticker"
SEND_TOOL_NAME = "send_sticker"


def _bigrams(text: str) -> list[str]:
    """中文没有空格，用 2-gram 折中。标点和空白当断点。"""
    cleaned = "".join(
        ch if ch.isalnum() or "\u4e00" <= ch <= "\u9fff" else " "
        for ch in str(text or "")
    )
    grams: list[str] = []
    for chunk in cleaned.split():
        if len(chunk) < 2:
            continue
        for i in range(len(chunk) - 1):
            grams.append(chunk[i:i + 2])
    return grams
