"""B 站扫码登录：让用户用 B 站 App 扫一下就带上登录态。

为什么必须登录：
    未登录能拿到的东西已经验证过了——搜索、详情、标签、弹幕、热门都行。
    缺的是两样：字幕（subtitles 返回空列表）和 AI 总结（直接 ResponseCode
    Exception）。字幕尤其重要，有字幕才能知道一条视频"到底讲了什么"，
    只靠标题和弹幕，她只能聊"标题看起来很有意思"，聊不进去。

为什么 cookie 单独存一个文件：
    business_config.json 是整个插件的业务配置，界面会读、会显示、会被
    用户手动改。把 SESSDATA 写进去等于把登录凭据摊在设置页上，而且每次
    保存设置都会把整份配置重写一遍。单独一个文件更干净，删了就等于退出
    登录。

二维码怎么给到用户：
    login_v2.QrCodeLogin 能拿到二维码图片的原始字节，这里转成 base64
    返回。调用方（宿主的 entry）把 base64 存成 png 给人扫就行，不需要
    插件自己有界面。
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from pathlib import Path
from typing import Any

#: 二维码有效期。B 站官方是 180 秒，这里判松一点，过期了让重新生成。
_QR_TTL_SECONDS = 180


def _maybe_bytes(picture: Any) -> bytes:
    """从库的 Picture 对象里抠出图片字节。

    这个版本的 Picture 可能是 dataclass（有 .content / .data），也可能是
    带 getvalue() 的 file-like，甚至直接就是 bytes。都试一遍，抠不到就
    返回空——上层会提示"二维码没生成出来"，而不是抛异常。
    """
    if picture is None:
        return b""
    if isinstance(picture, (bytes, bytearray)):
        return bytes(picture)
    for attr in ("content", "data", "raw", "_content"):
        value = getattr(picture, attr, None)
        if isinstance(value, (bytes, bytearray)) and value:
            return bytes(value)
    for method in ("getvalue", "read"):
        fn = getattr(picture, method, None)
        if callable(fn):
            try:
                value = fn()
                if isinstance(value, (bytes, bytearray)) and value:
                    return bytes(value)
            except Exception:
                pass
    return b""


async def _maybe_await(value: Any) -> Any:
    if asyncio.iscoroutine(value):
        return await value
    return value


class QQBilibiliLoginService:
    """维护一次登录会话。同一时间只允许一个二维码。"""

    CREDENTIAL_FILENAME = "bilibili_credential.json"

    def __init__(self, plugin: Any):
        self.plugin = plugin
        self._logger = plugin.logger
        self._qr: Any = None
        self._qr_created_at = 0.0
        self._last_state = ""

    # ── 凭据文件 ──

    def _path(self) -> Path:
        try:
            base = Path(self.plugin.data_path())
        except Exception:
            base = Path(".")
        return base / self.CREDENTIAL_FILENAME

    def load_credential(self) -> dict[str, Any]:
        path = self._path()
        try:
            if path.is_file():
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
        except Exception:
            self._logger.warning("[BiliLogin] 凭据文件读取失败", exc_info=True)
        return {}

    def save_credential(self, data: dict[str, Any]) -> None:
        path = self._path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(path)
            self._logger.info("[BiliLogin] 凭据已保存")
        except Exception:
            self._logger.warning("[BiliLogin] 凭据保存失败", exc_info=True)

    def clear_credential(self) -> bool:
        path = self._path()
        try:
            if path.is_file():
                path.unlink()
                return True
        except Exception:
            self._logger.warning("[BiliLogin] 删除凭据失败", exc_info=True)
        return False

    @staticmethod
    def _cookie_dict(cred: Any) -> dict[str, Any]:
        """把 Credential 拆成能存盘的 dict。

        get_cookies() 在版本之间返回值形态不一致（dict / list[dict] / str），
        所以先试它，拿不到再逐属性兜底。
        """
        out: dict[str, Any] = {}
        raw = None
        fn = getattr(cred, "get_cookies", None)
        if callable(fn):
            try:
                raw = fn()
            except Exception:
                raw = None

        if isinstance(raw, dict):
            out.update({str(k): str(v) for k, v in raw.items()})
        elif isinstance(raw, (list, tuple)):
            for item in raw:
                if isinstance(item, dict) and item.get("name"):
                    out[str(item["name"])] = str(item.get("value") or "")

        # 兜底：直接读属性（含私有命名）
        for key in ("sessdata", "bili_jct", "buvid3", "buvid4", "dedeuserid", "ac_time_value"):
            if out.get(key):
                continue
            for attr in (key, f"_{key}", f"__{key}"):
                value = getattr(cred, attr, None)
                if isinstance(value, str) and value:
                    out[key] = value
                    break
        return out

    # ── 扫码流程 ──

    async def start(self, *, force_new: bool = False) -> dict[str, Any]:
        """生成（或复用）一个登录二维码。

        复用逻辑：二维码还没过期、也没被别人扫走时不重复生成——用户可能
        刷新了一下就再来要一次，每次都换码会让人扫到一半失效。
        """
        if (
            not force_new
            and self._qr is not None
            and time.time() - self._qr_created_at < _QR_TTL_SECONDS
        ):
            try:
                pic = await _maybe_await(self._qr.get_qrcode_picture())
                data = _maybe_bytes(pic)
                if data:
                    return {
                        "ok": True,
                        "reused": True,
                        "expires_in": int(_QR_TTL_SECONDS - (time.time() - self._qr_created_at)),
                        "qrcode_base64": base64.b64encode(data).decode("ascii"),
                    }
            except Exception:
                pass

        try:
            from bilibili_api.login_v2 import QrCodeLogin

            qr = QrCodeLogin()
            await _maybe_await(qr.generate_qrcode())
            pic = await _maybe_await(qr.get_qrcode_picture())
            data = _maybe_bytes(pic)
            if not data:
                return {"ok": False, "reason": "二维码图片没生成出来（Picture 里取不到字节）"}
            self._qr = qr
            self._qr_created_at = time.time()
            self._last_state = "waiting"
            self._logger.info("[BiliLogin] 已生成登录二维码，等待扫码")
            return {
                "ok": True,
                "reused": False,
                "expires_in": _QR_TTL_SECONDS,
                "qrcode_base64": base64.b64encode(data).decode("ascii"),
            }
        except Exception as exc:
            self._logger.warning("[BiliLogin] 生成二维码失败", exc_info=True)
            return {"ok": False, "reason": f"{type(exc).__name__}: {exc}"[:200]}

    async def check(self) -> dict[str, Any]:
        """查一次扫码状态。成功后立刻保存凭据并热装载。"""
        if self._qr is None:
            return {"ok": False, "status": "none", "reason": "还没有生成二维码，先调用 start"}
        if time.time() - self._qr_created_at > _QR_TTL_SECONDS * 2:
            self._qr = None
            return {"ok": False, "status": "expired", "reason": "二维码已过期，重新生成"}

        try:
            event = await _maybe_await(self._qr.check_state())
        except Exception as exc:
            return {"ok": False, "status": "error", "reason": f"{type(exc).__name__}: {exc}"[:200]}

        # 枚举名在版本之间可能变（SCAN/SCANNED、CONF/CONFIRM、DONE/SUCCESS），
        # 不硬编码比较，统一转成大写字符串再判关键词。
        #
        # 注意 SCAN 的含义不可靠：2026-10-31 实测在完全没人扫码的情况下
        # check_state() 也返回 SCAN（B 站底层 86101「未扫码」和 86090「已扫
        # 未确认」在库这一层没分干净）。所以这里不把它说成"已扫码"，只报
        # "pending" 并且把原始 event 一起返回，让调用方自己决定怎么显示。
        name = str(getattr(event, "name", "") or event).upper()
        if "DONE" in name or "SUCCESS" in name:
            target = "done"
        elif "CONF" in name or "CONFIRM" in name:
            target = "confirmed"
        elif "SCAN" in name:
            target = "pending"
        elif "TIMEOUT" in name or "EXPIRE" in name:
            target = "timeout"
        else:
            target = "waiting"

        self._last_state = target
        if target != "done":
            return {"ok": True, "status": target, "event": name}

        try:
            cred = await _maybe_await(self._qr.get_credential())
        except Exception as exc:
            return {"ok": False, "status": "error", "reason": f"拿凭据失败: {exc}"[:200]}

        cookies = self._cookie_dict(cred)
        if not cookies.get("sessdata"):
            return {"ok": False, "status": "error", "reason": "凭据里没有 SESSDATA"}

        # 顺带把昵称和 mid 取回来，界面上能显示"已登录：xxx"
        name = ""
        mid = str(cookies.get("dedeuserid") or "")
        try:
            from bilibili_api.user import User

            if mid:
                info = await asyncio.wait_for(
                    User(int(mid)).get_user_info(), timeout=20.0,
                )
                name = str((info or {}).get("name") or "")
        except Exception:
            pass

        record = {
            **cookies,
            "uname": name,
            "mid": mid,
            "saved_at": time.time(),
        }
        self.save_credential(record)
        self._qr = None

        # 热装载：不让用户等下一轮刷取才生效
        browse = getattr(self.plugin, "bilibili_browse_service", None)
        if browse is not None:
            try:
                await browse.client.apply_credential(record)
            except Exception:
                self._logger.warning("[BiliLogin] 热装载登录态失败", exc_info=True)

        self._logger.info(f"[BiliLogin] 登录成功 user={name or '?'}")
        return {
            "ok": True,
            "status": "done",
            "name": name,
            "mid": mid,
            "has_buvid3": bool(cookies.get("buvid3")),
        }

    def describe(self) -> dict[str, Any]:
        data = self.load_credential()
        return {
            "logged_in": bool(data.get("sessdata")),
            "name": data.get("uname") or "",
            "mid": data.get("mid") or "",
            "saved_at": data.get("saved_at") or 0,
            "has_buvid3": bool(data.get("buvid3")),
            "pending_qr": self._qr is not None,
            "last_state": self._last_state,
            "credential_path": str(self._path()),
        }
