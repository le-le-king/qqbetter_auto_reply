"""QQ 表情包索引 —— 图存在 QQ 服务器上，这里只存指针。

## 为什么不是"下载下来存本地"

上一版是把图下载进 ``data/sticker/``，每张再跑一次 VLM 生成描述。问题有三个：

1. **费钱**。群里刷图，一张两秒、一次一个模型调用。一天几十张就是几十次
   调用，而且重启后又得重认一遍。
2. **费盘**。一张表情包 50~500KB，攒一年几个 G。
3. **不跨号**。换个 QQ 号就全没了。

## 现在存的是什么

QQ 的商城表情（``market_face`` 段）自带唯一标识：

    emoji_package_id  表情包 ID（数字）
    emoji_id          这张表情的 ID（hex 串）
    key               发送凭据
    url               一张永久地址（gxh.vip.qq.com/...raw300.gif）
    summary           QQ 给的预览文本

**这五个字段就是"存在 QQ 里"**：图本体一直在腾讯服务器，我们只留几十字节的
指针。发的时候把它原样塞回 ``mface`` 段，QQ 客户端自己从服务端拉图——不下载、
不上传、不走 Highway 通道。顺手还绕开了那台连不上的图片服务器
（``123.151.137.126:15000``，实测 TCP 超时，收藏表情加不进去就是它导致的）。

## 钱省在哪里

- 收到表情包：**只记标识，不识别**（0 成本）
- 要发某张时才认一次（``ensure_desc``），认完写进 JSON，**永久生效**
- 同一张图被转发二十次，只认一次

## 兼容

LLBot 上报的段类型是 ``market_face``，发送时用 ``mface``——两个名字都要认。
字段命名也是两套（``emoji_package_id`` / ``emojiPackageId``），统一在
``normalize_segment`` 里收口。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from pathlib import Path
from typing import Any, Optional

# 索引上限。一个热闹的群一天能刷几十张，不设上限一年上万条 JSON 就大得难受。
# 满了按「发过几次、多久没见过」淘汰——用得多的留下。
MAX_FACES = 800
# 一张表情包的描述上限。这句话会进提示词和工具返回，一涨就是几百字上下文。
MAX_DESC = 60
# 一次最多补几条描述。她一次调用只发一张，多认的是纯浪费。
DESCRIBE_BUDGET = 2

# 收到表情包时的段类型。LLBot = market_face，NapCat 老格式 = mface。
_IN_TYPES = ("market_face", "mface")

# QQ 的表情分两种，走的是完全不同的两条路，别混：
#
#   market_face  商城表情。带 emoji_id/key，能原样塞回 mface 段转出去（0 成本）。
#   image + subType=1
#                收藏表情。LLBot 解析 QQ 的 customFace 时直接降级成普通图片
#                （``elem.customFace → ElementType.Pic``），emoji_id/key 全丢，
#                只剩一个 md5 文件名和一条带 rkey 的 URL。这类**没法用 mface
#                重发**，只能把图存一份在本地。
#
# 实测：这台账号里的表情全是收藏表情，market_face 一条都没收到过。所以 B 路
# 不是兜底，是主力。


def _is_hex32(text: str) -> bool:
    return len(text) == 32 and all(c in "0123456789abcdef" for c in text.lower())


def _normalize_local(data: dict[str, Any]) -> Optional[dict[str, Any]]:
    """``image`` 段里挑出收藏表情。普通照片返回 None。"""
    try:
        sub_type = int(data.get("subType", data.get("sub_type", 0)) or 0)
    except (TypeError, ValueError):
        sub_type = 0
    if sub_type != 1:
        return None
    raw_file = str(data.get("file") or "").strip()
    url = str(data.get("url") or "").strip()
    name = raw_file.replace("\\", "/").rsplit("/", 1)[-1]
    stem, _dot, ext = name.partition(".")
    # QQ 的图片文件名就是这张图的 md5（大写）。同一张表情每次转发都带同一个
    # 文件名，所以拿它当主键比 URL 稳——URL 里的 rkey 几个小时就换一茬。
    md5 = stem.lower() if _is_hex32(stem) else ""
    if not md5:
        md5 = hashlib.md5((url or raw_file or "").encode("utf-8")).hexdigest()
    return {
        "mode": "local",
        "md5": md5,
        "url": url,
        "file": name,
        "ext": ("." + ext.lower()) if ext else ".jpg",
    }


def normalize_segment(segment: dict[str, Any]) -> Optional[dict[str, Any]]:
    """把两种命名的表情包段归一成同一份字段。不像表情包就返回 None。"""
    if not isinstance(segment, dict):
        return None
    seg_type = str(segment.get("type") or "")
    data = segment.get("data")
    if not isinstance(data, dict):
        return None
    if seg_type == "image":
        return _normalize_local(data)
    if seg_type not in _IN_TYPES:
        return None

    def pick(*names: str) -> Any:
        for name in names:
            value = data.get(name)
            if value not in (None, ""):
                return value
        return None

    emoji_id = str(pick("emoji_id", "emojiId") or "").strip()
    if not emoji_id:
        return None
    try:
        pkg = int(pick("emoji_package_id", "emojiPackageId") or 0)
    except (TypeError, ValueError):
        pkg = 0
    return {
        "mode": "mface",
        "emoji_id": emoji_id,
        "pkg": pkg,
        "key": str(pick("key") or "").strip(),
        "summary": str(pick("summary") or "").strip(),
        "url": str(pick("url") or "").strip(),
    }


class QQFaceIndex:
    """表情包标识索引。读写在内存，落盘异步去抖。"""

    FILENAME = "qq_faces.json"

    def __init__(self, plugin: Any, vision: Any = None):
        self.plugin = plugin
        self.vision = vision
        self._faces: dict[str, dict[str, Any]] = {}
        self._loaded = False
        self._lock = asyncio.Lock()
        self._dirty = False
        self._flush_task: Optional[asyncio.Task] = None
        self.diag = {
            "new": 0,
            "seen": 0,
            "described": 0,
            "describe_fail": 0,
            "send_ok": 0,
            "send_mface_fail": 0,
            "send_fallback": 0,
            "fetch_fail": 0,
        }

    # ── 路径 ────────────────────────────────────────────────

    @property
    def _path(self) -> Path:
        try:
            return Path(self.plugin.data_path()) / self.FILENAME
        except Exception:
            return Path(".") / self.FILENAME

    # ── 载入 / 落盘 ─────────────────────────────────────────

    async def start(self) -> None:
        await self._load()

    async def stop(self) -> None:
        task = self._flush_task
        self._flush_task = None
        if task and not task.done():
            task.cancel()
        if self._dirty:
            await asyncio.to_thread(self._write_sync)

    async def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        await asyncio.to_thread(self._load_sync)

    def _load_sync(self) -> None:
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception as exc:
            self.plugin.logger.warning(f"[Face] 索引读取失败: {exc}")
            return
        faces = payload.get("faces") if isinstance(payload, dict) else None
        if not isinstance(faces, dict):
            return
        for short, entry in faces.items():
            if not isinstance(entry, dict):
                continue
            item = dict(entry)
            item.setdefault("desc", "")
            item.setdefault("hits", 1)
            self._faces[str(short)] = item
        # 老索引（或者手工塞进来的条目）没有短编号，补上——不然提示词里
        # 会冒出 `[qNone]`，她照着写发不出去。
        for item in self._faces.values():
            if not item.get("nid"):
                item["nid"] = self._next_nid()
        if self._faces:
            known = sum(1 for e in self._faces.values() if str(e.get("desc") or "").strip())
            self.plugin.logger.info(
                f"[Face] 已载入 {len(self._faces)} 个 QQ 表情标识（{known} 个有描述）"
            )

    def _mark_dirty(self) -> None:
        self._dirty = True
        if self._flush_task and not self._flush_task.done():
            return
        try:
            self._flush_task = asyncio.create_task(self._delayed_flush())
        except RuntimeError:
            # 没有 running loop（同步上下文）→ 直接同步写，慢一点但不能丢。
            self._write_sync()
            self._dirty = False

    def _invalidate_prompt(self) -> None:
        """描述变了就得让提示词重读。只在**认出新描述**时调——每收到一张
        表情包就清一次缓存，等于每轮都重读一遍目录。"""
        instruction = getattr(self.plugin, "session_instruction_service", None)
        if instruction is not None:
            try:
                instruction._sticker_catalog_cache = ""
            except Exception:
                pass

    async def _delayed_flush(self) -> None:
        """收流路径上不能等磁盘。攒 3 秒写一次，刷屏时也只写一次。"""
        try:
            await asyncio.sleep(3.0)
            if self._dirty:
                await asyncio.to_thread(self._write_sync)
                self._dirty = False
        except asyncio.CancelledError:
            raise
        except Exception:
            self.plugin.logger.debug("[Face] 索引落盘失败", exc_info=True)

    def _write_sync(self) -> None:
        payload = {"version": 1, "faces": self._faces}
        path = self._path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)

    # ── 记 ──────────────────────────────────────────────────

    def remember(
        self,
        segment: dict[str, Any],
        *,
        sender_id: str = "",
        group_id: str = "",
    ) -> Optional[dict[str, Any]]:
        """收到表情包就记一笔。同步、不下载、不识别——收流路径上不能等。

        同一个 ``emoji_id`` 被转发多少次都只占一条，只累 ``hits``。
        """
        if not self._loaded:
            # 消息钩子可能在 startup 之前就来了（连接比加载快）。这里直接
            # 同步读盘：不读的话 `_mark_dirty` 会把空内存覆盖到磁盘上，
            # 之前攒的几百个表情标识一次清光。
            self._load_sync()
            self._loaded = True
        data = normalize_segment(segment)
        if not data:
            return None
        if data.get("mode") == "local":
            return self._remember_local(data, sender_id=sender_id, group_id=group_id)
        short = data["emoji_id"][:12]
        now = time.time()
        entry = self._faces.get(short)
        if entry is None:
            entry = {
                "nid": self._next_nid(),
                "mode": "mface",
                "eid": data["emoji_id"],
                "pkg": data["pkg"],
                "key": data["key"],
                "summary": data["summary"],
                "url": data["url"],
                "desc": "",
                "hits": 1,
                "first_seen": now,
                "last_seen": now,
                "sent": 0,
                "last_sent_at": 0.0,
                "from": str(sender_id or ""),
                "group": str(group_id or ""),
            }
            self._faces[short] = entry
            self.diag["new"] += 1
            self._trim()
        else:
            entry["hits"] = int(entry.get("hits") or 0) + 1
            entry["last_seen"] = now
            # key 每次上报的都不一样、会过期，必须用最新的那份。
            if data["key"]:
                entry["key"] = data["key"]
            if data["url"]:
                entry["url"] = data["url"]
            if data["pkg"]:
                entry["pkg"] = data["pkg"]
            # summary 常常是"[表情]"这种废话，别用它盖掉有用的旧值。
            if data["summary"] and "[" not in data["summary"]:
                entry["summary"] = data["summary"]
            self.diag["seen"] += 1
        self._mark_dirty()
        return dict(entry)

    def _remember_local(
        self,
        data: dict[str, Any],
        *,
        sender_id: str = "",
        group_id: str = "",
    ) -> Optional[dict[str, Any]]:
        """收藏表情：拿不到 emoji_id，只能靠图片 md5 认人。

        不下载、不识别——只是把"这张我见过、它的 URL 现在是这条"记下来。
        URL 里的 rkey 几小时就过期，所以每次上报都覆盖成最新的那份；
        真要发的时候再按这条 URL 去取图。
        """
        short = data["md5"][:12]
        now = time.time()
        entry = self._faces.get(short)
        if entry is None:
            entry = {
                "nid": self._next_nid(),
                "mode": "local",
                "eid": "",
                "md5": data["md5"],
                "ext": data.get("ext") or ".jpg",
                "file": data.get("file") or "",
                "url": data.get("url") or "",
                "pkg": 0,
                "key": "",
                "summary": "",
                "desc": "",
                "hits": 1,
                "first_seen": now,
                "last_seen": now,
                "sent": 0,
                "last_sent_at": 0.0,
                "from": str(sender_id or ""),
                "group": str(group_id or ""),
            }
            self._faces[short] = entry
            self.diag["new"] += 1
            self._trim()
        else:
            entry["hits"] = int(entry.get("hits") or 0) + 1
            entry["last_seen"] = now
            entry["mode"] = "local"
            entry["md5"] = data["md5"]
            if data.get("url"):
                entry["url"] = data["url"]
            if data.get("file"):
                entry["file"] = data["file"]
            if data.get("ext"):
                entry["ext"] = data["ext"]
            self.diag["seen"] += 1
        self._mark_dirty()
        return dict(entry)

    def _trim(self) -> None:
        if len(self._faces) <= MAX_FACES:
            return
        # 用得少、又很久没出现的先走。有描述的留一手——那是花过钱认出来的。
        ranked = sorted(
            self._faces.items(),
            key=lambda kv: (
                1 if str(kv[1].get("desc") or "").strip() else 0,
                int(kv[1].get("hits") or 0),
                float(kv[1].get("last_seen") or 0),
            ),
        )
        for short, _entry in ranked[: len(self._faces) - MAX_FACES]:
            self._faces.pop(short, None)

    def _next_nid(self) -> int:
        """提示词里的短编号。``q3`` 比 64 位 eid 好写，也不容易抄错。"""
        biggest = 0
        for entry in self._faces.values():
            try:
                biggest = max(biggest, int(entry.get("nid") or 0))
            except (TypeError, ValueError):
                continue
        return biggest + 1

    @staticmethod
    def _key_of(entry: dict[str, Any]) -> str:
        """索引主键。商城表情用 emoji_id，收藏表情用图片 md5。"""
        eid = str(entry.get("eid") or "")
        if eid:
            return eid[:12]
        return str(entry.get("md5") or "")[:12]

    def get(self, face_id: str) -> Optional[dict[str, Any]]:
        """按提示词里的 ``q3`` / 按 eid 前缀都能取到。"""
        token = str(face_id or "").strip().lower().lstrip("q")
        if not token:
            return None
        for entry in self._faces.values():
            if str(entry.get("nid") or "") == token:
                return self._public(entry)
            if str(entry.get("eid") or "").startswith(token):
                return self._public(entry)
            if str(entry.get("md5") or "").startswith(token):
                return self._public(entry)
        return None

    def catalog_lines(self, *, limit: int = 24) -> tuple[list[str], int]:
        """生成提示词里的表情包目录。返回 (行列表, 还没认过的张数)。

        只列**有描述的**：没描述的她自己都不知道是什么，写进目录也只是占
        上下文。没认过的那部分给一个数字，让她知道还有货没拆。
        """
        described = [e for e in self._faces.values() if str(e.get("desc") or "").strip()]
        described.sort(key=lambda e: float(e.get("last_seen") or 0), reverse=True)
        lines = [
            f"    [q{e.get('nid')}] {str(e.get('desc') or '')}"
            for e in described[:limit]
        ]
        return lines, max(0, len(self._faces) - len(described))

    # ── 找 ──────────────────────────────────────────────────

    def search(self, query: str = "", *, limit: int = 3) -> list[dict[str, Any]]:
        """按她那句话找。空 query = 挑最久没发过的。

        **只在有描述的里面找**：描述为空说明她根本不知道这张是什么，选中了
        也只能盲发。没描述的条目先留着（下次同一张上报会刷新 URL），等
        ``ensure_desc`` 认过才进候选池。
        """
        entries = [
            e for e in self._faces.values() if str(e.get("desc") or "").strip()
        ]
        if not entries:
            return []
        text = str(query or "").strip()
        if not text:
            ordered = sorted(entries, key=lambda e: float(e.get("last_sent_at") or 0))
            return [self._public(e) for e in ordered[:limit]]

        scored: list[tuple[float, dict[str, Any]]] = []
        for entry in entries:
            score = self._score(text, entry)
            if score > 0:
                scored.append((score, entry))
        if not scored:
            return []
        scored.sort(
            key=lambda pair: (pair[0], -float(pair[1].get("last_sent_at") or 0)),
            reverse=True,
        )
        return [self._public(e) for _s, e in scored[:limit]]

    @staticmethod
    def _score(query: str, entry: dict[str, Any]) -> float:
        desc = str(entry.get("desc") or "").lower()
        summary = str(entry.get("summary") or "").lower()
        q = str(query or "").lower().strip()
        if not q:
            return 0.0
        if desc and q in desc:
            return 6.0
        if summary and q in summary:
            return 3.0
        hits = 0
        for gram in _bigrams(q):
            if (desc and gram in desc) or (summary and gram in summary):
                hits += 1
        return min(hits, 4) * 0.7

    @staticmethod
    def _public(entry: dict[str, Any]) -> dict[str, Any]:
        nid = entry.get("nid")
        ident = f"q{nid}" if nid else str(entry.get("eid") or "")[:12]
        return {"id": ident, **entry}

    def list_all(self, *, limit: int = 10, only_described: bool = False) -> list[dict[str, Any]]:
        items = list(self._faces.values())
        if only_described:
            items = [e for e in items if str(e.get("desc") or "").strip()]
        items.sort(key=lambda e: float(e.get("last_seen") or 0), reverse=True)
        return [self._public(e) for e in items[:limit]]

    def label(self, entry: dict[str, Any]) -> str:
        """给它一个能念出来的名字。有描述用描述，没有就用 QQ 的 summary。"""
        desc = str(entry.get("desc") or "").strip()
        if desc:
            return desc
        summary = str(entry.get("summary") or "").strip()
        if summary and "[" not in summary:
            return summary
        return ""

    def stats(self) -> dict[str, Any]:
        described = sum(1 for e in self._faces.values() if str(e.get("desc") or "").strip())
        return {"total": len(self._faces), "described": described, **self.diag}

    # ── 认（唯一花钱的地方）─────────────────────────────────

    async def ensure_desc(self, entry: dict[str, Any]) -> str:
        """给一张表情包补描述。已经有就直接返回——这是省钱的核心。"""
        if not isinstance(entry, dict):
            return ""
        await self._load()
        short = self._key_of(entry)
        stored = self._faces.get(short) or entry
        desc = str(stored.get("desc") or "").strip()
        if desc:
            return desc
        if self.vision is None:
            return ""
        url = str(stored.get("url") or "").strip()
        if not url:
            self.diag["describe_fail"] += 1
            return ""
        text = ""
        # 先看这张有没有以"普通图片"的身份被认过。收藏表情在 LLBot 上报时
        # 就是一张普通图片，聊天时已经描述过一遍了——那是花掉的钱，直接
        # 拿来用，不再付第二遍。peek 只查缓存，不会回源。
        peek = getattr(self.vision, "peek", None)
        if callable(peek):
            try:
                text = str(await peek(url, kind="image") or "").strip()[:MAX_DESC]
            except Exception:
                text = ""
        if not text:
            try:
                meta = await self.vision.describe_sticker(url) or {}
            except Exception:
                self.diag["describe_fail"] += 1
                return ""
            text = self._compose(meta)
        if not text:
            self.diag["describe_fail"] += 1
            return ""
        async with self._lock:
            target = self._faces.get(short)
            if target is not None:
                target["desc"] = text
                target["described_at"] = time.time()
                self._mark_dirty()
        self.diag["described"] += 1
        self._invalidate_prompt()
        self.plugin.logger.info(f"[Face] 认出第 {self.diag['described']} 张: {text[:30]}")
        # 认的时候图已经下载过一次了，顺手存一份到本地。QQ 的图片 URL
        # 带 rkey，几个小时后就是一条死链——她隔天想发这张就发不出去了。
        # 下载是免费的，存下来就一劳永逸。不 await：这会儿她正等着回话。
        try:
            asyncio.create_task(self._ensure_file(stored))
        except RuntimeError:
            pass
        return text

    @staticmethod
    def _compose(meta: dict[str, str]) -> str:
        """情绪放最前——它是检索的高频词（"无语""嘲讽"），画面描述太长反而会
        把有用的部分挤出上限。"""
        parts: list[str] = []
        for key in ("emotion", "scene", "usage"):
            value = str(meta.get(key) or "").strip()
            if value and value not in parts:
                parts.append(value)
        return " / ".join(parts)[:MAX_DESC]

    def add_note(self, entry: dict[str, Any], note: str) -> str:
        """她自己给这张起的名字，放在描述最前面。

        她说的话比模型看的准——"这个用来忽悠我妈"这种信息，VLM 永远看不出来。
        """
        text = str(note or "").strip()
        if not isinstance(entry, dict):
            return ""
        short = self._key_of(entry)
        stored = self._faces.get(short)
        if stored is None:
            return ""
        desc = str(stored.get("desc") or "").strip()
        if text and text not in desc:
            desc = f"{text} / {desc}".strip(" /")[:MAX_DESC]
            stored["desc"] = desc
            self._mark_dirty()
            self._invalidate_prompt()
        return desc

    async def describe_recent(self, *, limit: int = DESCRIBE_BUDGET) -> int:
        """补最近几张的描述。只在工具显式要求时调，一次最多两张。"""
        await self._load()
        pending = [
            e for e in self._faces.values() if not str(e.get("desc") or "").strip()
        ]
        pending.sort(key=lambda e: float(e.get("last_seen") or 0), reverse=True)
        done = 0
        for entry in pending[: max(1, limit)]:
            if await self.ensure_desc(entry):
                done += 1
        return done

    # ── 发 ──────────────────────────────────────────────────

    async def send(
        self,
        entry: dict[str, Any],
        *,
        user_id: str = "",
        group_id: str = "",
    ) -> bool:
        """把表情包原样转出去。

        首选 ``mface``：只是把五个标识字段塞回给 QQ，图由客户端从服务端拉，
        不经过本机的图片上传通道。``key`` 过期时 mface 会失败，这时退化成
        发图片 URL——图还能到，只是变成一张普通图。
        """
        await self._load()
        if not isinstance(entry, dict):
            return False
        short = self._key_of(entry)
        stored = self._faces.get(short) or entry
        client = getattr(self.plugin, "qq_client", None)
        if client is None:
            return False

        # 收藏表情（image+subType=1）拿不到 emoji_id，塞不出 mface 段。
        # 只能把图弄到本地再发。下载是免费的，花钱的识别只在 ensure_desc。
        if str(stored.get("mode") or "") == "local" or not str(stored.get("eid") or ""):
            message_id = await self._send_local(
                stored, user_id=user_id, group_id=group_id, client=client,
            )
            if not message_id:
                return False
            self.diag["send_ok"] += 1
            self._mark_sent(short)
            return True

        segment = {
            "type": "mface",
            "data": {
                "emoji_package_id": int(stored.get("pkg") or 0),
                "emoji_id": str(stored.get("eid") or ""),
                "key": str(stored.get("key") or ""),
                "summary": str(stored.get("summary") or ""),
            },
        }
        message_id = None
        try:
            if group_id:
                message_id = await client.send_group_message_segments(
                    group_id, [segment], record_sent=False,
                )
            elif user_id:
                message_id = await client.send_private_message_segments(
                    user_id, [segment], record_sent=False,
                )
        except Exception as exc:
            self.plugin.logger.info(
                f"[Face] mface 发送失败（会用图片兜底）: {type(exc).__name__}: {exc}"
            )
            message_id = None

        if not message_id:
            self.diag["send_mface_fail"] += 1
            url = str(stored.get("url") or "").strip()
            if not url:
                return False
            image = {"type": "image", "data": {"file": url, "sub_type": "1"}}
            try:
                if group_id:
                    message_id = await client.send_group_message_segments(
                        group_id, [image], record_sent=False,
                    )
                elif user_id:
                    message_id = await client.send_private_message_segments(
                        user_id, [image], record_sent=False,
                    )
            except Exception:
                message_id = None
            if message_id:
                self.diag["send_fallback"] += 1

        if not message_id:
            return False

        self.diag["send_ok"] += 1
        self._mark_sent(short)
        return True

    def _mark_sent(self, short: str) -> None:
        target = self._faces.get(short)
        if target is None:
            return
        target["sent"] = int(target.get("sent") or 0) + 1
        target["last_sent_at"] = time.time()
        self._mark_dirty()

    async def _send_local(
        self,
        stored: dict[str, Any],
        *,
        user_id: str,
        group_id: str,
        client: Any,
    ) -> Optional[str]:
        """收藏表情：先把图落到本地，再用 ``file:///`` 发出去。

        发出去的是**一张普通图片**。QQ 客户端收到后不会显示成表情包样式，
        但内容一模一样——她没有 market_face 标识，能做到这一步就是极限了。
        """
        path = await self._ensure_file(stored)
        if path is None:
            return None
        # NapCat 只认三斜杠 + 正斜杠。裸 Windows 路径发出去 OneBot 那边一个字
        # 都不回，请求卡到超时（2026-11-01 实测）。
        locator = "file:///" + str(path).replace("\\", "/")
        segment = {"type": "image", "data": {"file": locator, "sub_type": "1"}}
        try:
            if group_id:
                return await client.send_group_message_segments(
                    group_id, [segment], record_sent=False,
                )
            if user_id:
                return await client.send_private_message_segments(
                    user_id, [segment], record_sent=False,
                )
        except Exception as exc:
            self.diag["send_fallback"] += 1
            self.plugin.logger.info(
                f"[Face] 本地图发送失败: {type(exc).__name__}: {exc}"
            )
        return None

    @property
    def _faces_dir(self) -> Path:
        try:
            return Path(self.plugin.data_path()) / "faces"
        except Exception:
            return Path(".") / "faces"

    async def _ensure_file(self, entry: dict[str, Any]) -> Optional[Path]:
        """拿到这张表情的本地文件。有就用，没有才按 URL 下一次。

        下载**不花钱**（流量），所以这里不设上限反复下载的担心是多余的；
        但也不主动下——收到时不下载，等她真要用才下，省掉一堆永远用不上
        的图占盘。
        """
        md5 = str(entry.get("md5") or "").strip()
        if not md5:
            return None
        ext = str(entry.get("ext") or ".jpg")
        path = self._faces_dir / f"{md5}{ext}"
        try:
            if path.exists() and path.stat().st_size > 0:
                return path
        except OSError:
            pass
        url = str(entry.get("url") or "").strip()
        if not url or self.vision is None:
            self.diag["fetch_fail"] += 1
            return None
        raw = await self.vision.fetch_bytes(url)
        if not raw:
            self.diag["fetch_fail"] += 1
            return None
        try:
            await asyncio.to_thread(self._write_bytes, path, raw)
        except Exception as exc:
            self.diag["fetch_fail"] += 1
            self.plugin.logger.info(f"[Face] 表情落盘失败: {type(exc).__name__}")
            return None
        return path

    @staticmethod
    def _write_bytes(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def _bigrams(text: str) -> list[str]:
    """中文没空格，2-gram 折中。标点和空白当断点。"""
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
