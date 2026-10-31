"""qq_miyo_codes —— 米哈游三款游戏前瞻直播兑换码，自动发群。

为什么需要它
-----------
原神 / 崩坏：星穹铁道 / 绝区零 每个版本前瞻直播（约每 6 周一次）会发 3 个兑换码，
通常次日 23:59 失效。群里那几位经常是第二天才看到、才想起要换。

它做什么
-------
1. 定时轮询米游社直播间自己用的那个接口（见 miyolive.py），拿官方原始码；
2. 只在「码已揭晓（to_get_time 到了）且直播已结束」之后才发，避免发半截；
3. 同一场直播只发一次（按 act_id 去重，状态落在 state_file）；
4. 群里谁把直播间链接贴进来，自动接住里面的 act_id，不用改配置。

act_id 从哪来（这是唯一没法全自动的一环）
---------------------------------------
米哈游没有「列出所有前瞻」的接口，每场直播的 act_id（ea 开头那串）只能从外面拿。
所以按优先级取：配置里手填 > 群里接住的链接 > 上次用过的。
配置里手填最简单：每次前瞻前把浏览器地址栏 ?act_id= 后面那串抄进来，10 秒的事。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.event.filter import EventMessageType
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register

from .miyolive import (
    GAMES,
    MiyoError,
    build_message,
    fetch_codes,
    fetch_index,
    fetch_nav_act_id,
    fetch_notice,
    fetch_show_meta,
    fetch_version_span,
    parse_act_id,
    supports_version_span,
    parse_dt,
)

PLUGIN_NAME = "astrbot_plugin_miyo_codes"

DEFAULTS = {
    "enabled": True,
    # AstrBot 里这个平台实例的 id（面板 → 平台 → 名字），用来拼 unified_msg_origin。
    # 留空 = 自动取第一个「支持主动推送」的实例。多平台同时在线时才需要手填。
    "platform_id": "",
    # 发到哪些群（群号）。故意留空：装上就往陌生群里发消息是最不能接受的事
    "target_groups": [],
    # 发到哪些人的私聊（QQ 号）。走的是好友私聊，得先加机器人为好友
    "target_friends": [],
    "games": ["hkrpg", "ys", "nap"],
    # "hkrpg=ea202610261626345889" 一行一个
    "act_overrides": "",
    # 直播窗口内多久看一眼（秒）。窗口外按 30 分钟一次慢速扫
    "poll_interval_sec": 180,
    "slow_poll_interval_sec": 1800,
    # 只在这个时段高频轮询，其余时间慢速
    "active_hours": "17:00-23:59",
    # 直播结束后再稳这么久才发（码通常在结束前后才齐，太早发会缺码）
    "post_delay_min": 30,
    # 要等接口里的码「全部揭晓」才发。绝区零一般只发 1 个码，
    # 所以不能写死"至少 3 个"，否则绝区零永远发不出去。
    "require_all_codes": True,
    # 万一某个码一直不揭晓，等这么久之后就先发已拿到的（0 = 一直等）
    "partial_grace_min": 60,
    # 至少要有几个码（下限，防止接口抽风返回 1 个残缺数据就发）
    "min_codes": 1,
    # 直播前多久在群里提醒"把直播间链接丢进来"（0 = 不提醒）
    "remind_before_min": 30,
    # 前瞻当天、又没拿到 act_id 时，在群里说一句求链接
    "ask_for_link": False,
    # 自动从米游社版区工具列表里发现 act_id。
    # 「直播兑换码」按钮只在前瞻前后约 29 小时出现，平时查不到是正常的。
    # 目前只有星铁版区配了这一项，另外两家会一直查不到，不影响别的路径
    "nav_auto": True,
    # 群里有人说「崩铁兑换码」这类话时，直接把码回在当场（不广播到别的群）
    "ask_game_codes": True,
    # 允许用 /兑码 强制重发的 QQ 号。留空 = 群管理员和私聊可用，普通群成员只回状态。
    # 收着的原因：/兑码 会把三个游戏的码重新广播一遍，谁都能刷就成了刷屏器
    "admin_users": [],
    # 状态文件路径。留空 = 落在 AstrBot 自己的 data/plugin_data/<插件名>/state.json，
    # 跟着数据目录走，换机器、换系统路径都不用改。确有需要再手填绝对路径。
    "state_file": "",
    # 直播开始超过这么多小时就不再发（防止把旧场次的码当新的发出去）
    "stale_hours": 48,
}

CMD = "兑码"

# 群里最常用的叫法。既用来识别「崩铁兑换码」这类指令，也用来拼提醒文案。
GAME_ALIASES = {
    "原神": "ys",
    "星铁": "hkrpg",
    "崩铁": "hkrpg",
    "星穹铁道": "hkrpg",
    "崩坏星穹铁道": "hkrpg",
    "绝区零": "nap",
    "zzz": "nap",
}
GAME_ASK_WORD = {"hkrpg": "崩铁", "ys": "原神", "nap": "绝区零"}

# 「崩铁兑换码」这类问法。装饰器和处理函数必须用同一个，否则会出现
# 外层放进来、内层又判不过（或者反过来）的哑巴情况。
# 前面不许有别的内容，后面允许挂「是什么 / 谢谢 / ？」这些尾巴——
# 群里没人会一字不差地只发四个字。
ASK_CODES_RE = (
    r"^\s*(原神|星铁|崩铁|崩坏星穹铁道|星穹铁道|绝区零|zzz)\s*(的)?\s*兑换码"
    r"\s*(是什么|是啥|多少|有吗|还有吗|呢|呀|啊|谢谢|谢谢啦)?\s*[?？!！。.~～]*\s*$"
)


def _now() -> float:
    return time.time()


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def _in_window(spec: str) -> bool:
    """'17:00-23:59' 这种窗口判断，支持跨零点。"""
    spec = (spec or "").strip()
    if not spec or "-" not in spec:
        return True
    try:
        a, b = spec.split("-", 1)
        sh, sm = (int(x) for x in a.strip().split(":"))
        eh, em = (int(x) for x in b.strip().split(":"))
    except Exception:  # noqa: BLE001
        return True
    now = time.localtime()
    cur = now.tm_hour * 60 + now.tm_min
    start, end = sh * 60 + sm, eh * 60 + em
    if start <= end:
        return start <= cur <= end
    return cur >= start or cur <= end


def _hash_codes(codes: list[dict]) -> str:
    body = "|".join(sorted(c["code"] for c in codes))
    return hashlib.sha1(body.encode("utf-8")).hexdigest()[:16]


class State:
    """很薄的一层持久化：哪些场次发过了、每个游戏当前的 act_id。"""

    def __init__(self, path: str):
        self.path = path
        self.data = {"acts": {}, "sent": {}, "asked": {}, "schedule": {}, "meta": {}}
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as fp:
                raw = json.load(fp)
            if isinstance(raw, dict):
                for k in ("acts", "sent", "asked", "schedule", "meta"):
                    if not isinstance(raw.get(k), dict):
                        raw[k] = {}
                self.data = raw
        except FileNotFoundError:
            pass
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[miyo_codes] 状态文件读不动，按空的算：{exc}")

    def save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fp:
                json.dump(self.data, fp, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[miyo_codes] 状态文件写不进去：{exc}")

    # --- acts ---
    def get_act(self, game: str) -> str:
        return str((self.data["acts"].get(game) or {}).get("act_id") or "")

    def set_act(self, game: str, act_id: str, source: str) -> None:
        self.data["acts"][game] = {
            "act_id": act_id,
            "source": source,
            "ts": int(_now()),
        }
        self.save()

    # --- sent ---
    # 发送对象统一用 "g:群号" / "f:QQ号" 表示，群和私聊走同一套去重。
    def sent_targets(self, act_id: str, sig: str) -> set[str]:
        """这一场（同内容指纹）已经发到了哪些地方。

        返回 {"*"} 表示老格式记录「当时配置的对象都发过」，用来看作"不用再发"。
        """
        rec = self.data["sent"].get(act_id) or {}
        if not rec or rec.get("sig") != sig:
            return set()
        targets = rec.get("targets")
        if targets is not None:
            return {str(t) for t in targets}
        groups = rec.get("groups")
        if groups is None:
            return {"*"}
        return {f"g:{g}" for g in groups}

    def mark_sent(
        self, act_id: str, game: str, sig: str, codes: list[dict], targets: list[str]
    ) -> None:
        rec = self.data["sent"].get(act_id) or {}
        # 老记录只有 groups、没有 targets，这里必须走一遍 sent_targets 把它翻成
        # "g:群号"，否则这一次写入会把「哪些群已经收到」丢掉，下一轮又会重发一遍。
        prev = (
            {t for t in self.sent_targets(act_id, sig) if t != "*"}
            if rec.get("sig") == sig
            else set()
        )
        self.data["sent"][act_id] = {
            "sig": sig,
            "game": game,
            "codes": [c["code"] for c in codes],
            "targets": sorted(prev | {str(t) for t in targets}),
            "ts": int(_now()),
        }
        self.save()

    # --- asked ---
    def already_asked(self, key: str) -> bool:
        return bool(self.data["asked"].get(key))

    def mark_asked(self, key: str) -> None:
        self.data["asked"][key] = int(_now())
        self.save()

    # --- 下一场前瞻（从官方预告帖读来的）---
    def get_schedule(self, game: str) -> dict:
        return dict(self.data["schedule"].get(game) or {})

    def set_schedule(self, game: str, info: dict) -> None:
        self.data["schedule"][game] = {
            "live_at": float(info.get("live_at") or 0),
            "live_at_text": str(info.get("live_at_text") or ""),
            "version": str(info.get("version") or ""),
            "subject": str(info.get("subject") or ""),
            "post_id": str(info.get("post_id") or ""),
            "ts": int(_now()),
        }
        self.save()

    # --- 其它零散状态 ---
    def meta_get(self, key: str, default=None):
        return self.data["meta"].get(key, default)

    def meta_set(self, key: str, value) -> None:
        self.data["meta"][key] = value
        self.save()


@register(
    PLUGIN_NAME,
    "miyo_codes",
    "原神 / 星穹铁道 / 绝区零 前瞻直播兑换码：官方接口取码，直播结束当天自动发群/私聊",
    "1.7.0",
)
class MiyoLiveCodes(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        cfg = {**DEFAULTS, **(config or {})}
        # 清掉插件配置里被写成空字符串的列表（面板上删光会留空串）
        for key in ("target_groups", "target_friends", "games"):
            if isinstance(cfg.get(key), str):
                cfg[key] = [x.strip() for x in cfg[key].split(",") if x.strip()]
        self.cfg = cfg
        self._auto_platform: str | None = None  # 自动探测到的平台实例，探一次就记住
        self._platform_try_at: float = 0.0  # 上次探测平台的时间，失败时按节流重试
        self.state = State(self._state_path())
        self._task: asyncio.Task | None = None
        self._last_error: str = ""
        # 同一场次的发送必须串行：定时轮询和群里的指令可能同时进来，
        # 不加锁两边都会读到"还没发过"，群里就会收到两条一模一样的码
        self._locks: dict[str, asyncio.Lock] = {}
        # 「XX兑换码」指令的冷却，key = 群号|游戏，防同一个人连点刷屏
        self._ask_at: dict[str, float] = {}

        if cfg.get("enabled", True):
            coro = self._loop()
            try:
                self._task = asyncio.create_task(coro)
            except RuntimeError:
                # 没有运行中的事件循环（离线自测）。不 close 会留一条 never awaited 警告
                coro.close()
                self._task = None

    # ------------------------------------------------------------------ 路径与平台
    def _state_path(self) -> str:
        """状态文件放哪。

        AstrBot 给每个插件留了固定数据目录 data/plugin_data/<插件名>/，
        跟着它走就不会因为换机器、换系统、升级丢历史。配置里手填了就用填的。
        """
        raw = str(self.cfg.get("state_file") or "").strip()
        if raw:
            return raw
        try:
            from astrbot.core.star.star_tools import StarTools

            base = str(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception as exc:  # noqa: BLE001
            # 拿不到数据目录（AstrBot 版本较老 / 权限不足）时退回插件自己的目录。
            # 一个路径问题不该把整个插件弄崩。
            logger.debug(f"[miyo_codes] 取插件数据目录失败，改用插件目录：{exc}")
            base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "plugin_data")
        return os.path.join(base, "state.json")

    def _platform(self) -> str:
        """发消息用哪个平台实例。

        别人的实例名千奇百怪（aiocqhttp / qq_main / snowluma…），
        写死一个名字的结果是「插件装得上、消息发不出去」，所以支持自动探测。

        探测失败**不能缓存**：平台可能只是比插件晚连上几十秒。缓存了空串，
        表现就是插件一切正常、日志只有一条 warning、但一条消息都发不出去，
        非得重载才能恢复。这里改成失败后 60 秒再试。
        """
        want = str(self.cfg.get("platform_id") or "").strip()
        if want:
            return want
        if self._auto_platform:
            return self._auto_platform
        now = _now()
        if now - self._platform_try_at < 60:
            return ""
        self._platform_try_at = now
        found = self._detect_platform()
        if found:
            self._auto_platform = found
        return found

    def _detect_platform(self) -> str:
        """从已加载的平台实例里挑一个「能主动推送」的。"""
        try:
            insts = list(self.context.platform_manager.get_insts())
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[miyo_codes] 取平台实例失败，请手动填 platform_id：{exc}")
            return ""
        if not insts:
            logger.warning("[miyo_codes] 暂时没有平台实例，等平台连上再发")
            return ""

        usable: list[tuple[str, str]] = []
        for inst in insts:
            try:
                meta = inst.meta()
            except Exception:  # noqa: BLE001
                continue
            pid = str(getattr(meta, "id", "") or "")
            if not pid:
                continue
            # 只能被动回复的适配器（比如某些网页端）发不了定时推送
            if not getattr(meta, "support_proactive_message", True):
                continue
            usable.append((pid, str(getattr(meta, "name", "") or "")))

        if not usable:
            logger.warning("[miyo_codes] 没有支持主动推送的平台实例，请检查平台配置")
            return ""
        if len(usable) > 1:
            names = "、".join(f"{n}({p})" for p, n in usable)
            logger.info(
                f"[miyo_codes] 有多个平台实例可用：{names}；"
                f"默认用「{usable[0][0]}」，要换请在配置里填 platform_id"
            )
        return usable[0][0]

    # ------------------------------------------------------------------ 配置
    def _overrides(self) -> dict[str, str]:
        out: dict[str, str] = {}
        for line in str(self.cfg.get("act_overrides") or "").splitlines():
            line = line.strip()
            if not line or "=" not in line:
                continue
            game, act = line.split("=", 1)
            game = game.strip()
            if game in GAMES:
                act = parse_act_id(act) or act.strip()
                if act:
                    out[game] = act
        return out

    def _act_for(self, game: str) -> str:
        override = self._overrides().get(game)
        if override:
            return override
        return self.state.get_act(game)

    def _games(self) -> list[str]:
        raw = self.cfg.get("games") or list(GAMES.keys())
        return [g for g in raw if g in GAMES] or list(GAMES.keys())

    def _groups(self) -> list[str]:
        raw = self.cfg.get("target_groups") or []
        return [str(g).strip() for g in raw if str(g).strip()]

    def _friends(self) -> list[str]:
        raw = self.cfg.get("target_friends") or []
        return [str(q).strip() for q in raw if str(q).strip()]

    def _targets(self) -> list[str]:
        """所有发送对象，群和私聊统一成 "g:群号" / "f:QQ号"。"""
        return [f"g:{g}" for g in self._groups()] + [f"f:{q}" for q in self._friends()]

    def _umo(self, key: str) -> str:
        """g:123456 → 平台:GroupMessage:123456；f:123456 → 平台:FriendMessage:123456

        拿不到平台实例时返回空串，由调用方跳过并提示，不要把 "":GroupMessage:123
        这种东西丢给 send_message 去炸。
        """
        platform = self._platform()
        if not platform:
            return ""
        kind, _, ident = str(key).partition(":")
        mtype = "FriendMessage" if kind == "f" else "GroupMessage"
        return f"{platform}:{mtype}:{ident}"

    @staticmethod
    def _key_label(key: str) -> str:
        if ":" not in str(key):
            return str(key)  # "（旧记录）" 这类非标准值原样返回
        kind, _, ident = str(key).partition(":")
        return f"私聊{ident}" if kind == "f" else f"群{ident}"

    @staticmethod
    def _mask_num(ident: str) -> str:
        ident = str(ident)
        return ident if len(ident) <= 5 else f"{ident[:3]}***{ident[-3:]}"

    @classmethod
    def _mask(cls, key: str) -> str:
        """日志专用的脱敏标签。

        群号和 QQ 号在自己屏幕上看得见没问题，但用户把日志贴到 issue 求助是常事，
        别人的号码不该跟着公开。所以日志一律用这个，指令回复仍用全号。
        """
        if ":" not in str(key):
            return str(key)  # "（旧记录）" 这类非标准值原样返回
        kind, _, ident = str(key).partition(":")
        short = cls._mask_num(ident)
        return f"私聊{short}" if kind == "f" else f"群{short}"

    def _near_live(self, game: str, now: float) -> bool:
        """现在是不是离这场直播很近（开播前半小时 ~ 结束后 6 小时）。"""
        sched = self.state.get_schedule(game)
        live_at = float(sched.get("live_at") or 0)
        return bool(live_at) and live_at - 1800 <= now <= live_at + 6 * 3600

    def _lock_for(self, act_id: str) -> asyncio.Lock:
        """同一场次共用一把锁。不同场次、不同游戏互不影响。"""
        lock = self._locks.get(act_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[act_id] = lock
        return lock

    def _should_poll_fast(self) -> bool:
        """该不该高频轮询。

        优先看预告：直播可能在任何时段——周五周六都有过，原神春节档还放在中午 12:00。
        所以只要手里有预告，就按预告的开播时间前后拉高频率，不看钟点。

        注意这里只认「还没过完」的预告。直播结束一个月后那条记录还躺在状态里，
        若按「有记录就算有预告」判断，active_hours 那个兜底就永远走不到，插件会
        被一条陈年预告永久锁在慢速模式。
        """
        now = _now()
        games = self._games()
        if any(self._near_live(g, now) for g in games):
            return True
        for g in games:
            live_at = float(self.state.get_schedule(g).get("live_at") or 0)
            if live_at and live_at + 6 * 3600 >= now:
                # 预告还在未来（或刚结束不到 6 小时）：按预告走，不按钟点
                return False
        return _in_window(str(self.cfg.get("active_hours") or ""))

    # ------------------------------------------------------------------ 主循环
    async def _loop(self) -> None:
        await asyncio.sleep(10)  # 等平台连上，别在启动瞬间抢发
        while True:
            wait = float(self.cfg.get("slow_poll_interval_sec") or 1800)
            try:
                if self._should_poll_fast():
                    wait = float(self.cfg.get("poll_interval_sec") or 180)
                await self._tick()
            except Exception as exc:  # noqa: BLE001
                logger.exception("[miyo_codes] 轮询出错")
                self._last_error = str(exc)
            await asyncio.sleep(max(30.0, wait))

    async def _tick(self) -> None:
        if not self.cfg.get("enabled", True):
            return
        await self._refresh_nav_act()
        await self._refresh_schedule()
        await self._refresh_span()
        missing: list[str] = []
        for game in self._games():
            act = self._act_for(game)
            if not act:
                missing.append(game)
                await self._maybe_remind(game)
                continue
            try:
                await self._check_game(game, act, source="auto")
            except MiyoError as exc:
                self._last_error = f"{game}: {exc}"
                logger.info(f"[miyo_codes] {game} 暂时没结果：{exc}")
            except Exception as exc:  # noqa: BLE001
                self._last_error = f"{game}: {exc}"
                logger.exception(f"[miyo_codes] {game} 检查失败")
        if missing and self.cfg.get("ask_for_link") and _in_window(
            str(self.cfg.get("active_hours") or "")
        ):
            await self._ask_for_link(missing)

    def _notice_interval(self) -> float:
        """多久看一次预告。

        平时 6 小时足够——预告帖一般提前 3~7 天发，跑不掉。
        但临近「按版本周期推算出来的前瞻窗口」时缩到 1 小时：万一官方是当天
        才补发预告，6 小时的间隔足以把整场直播直接错过。
        """
        now = _now()
        for game in self._games():
            span = self.state.meta_get(f"span:{game}") or {}
            end_ts = float(span.get("end_ts") or 0)
            if not end_ts or now >= end_ts:
                continue
            guess = end_ts - 12 * 86400  # 前瞻通常在上线前 8~13 天
            if guess - 3 * 86400 <= now <= guess + 5 * 86400:
                return 3600.0
        return 6 * 3600.0

    async def _refresh_nav_act(self) -> None:
        """从米游社版区工具列表里自动发现 act_id。

        这是目前唯一不用人给、也不用登录的来源：官方的「直播兑换码」按钮一上线，
        它的跳转链接里就带着 act_id。按钮只在直播前后约 29 小时存在，所以
        平时查不到是正常的 —— 不能因为查不到就当成失败。

        频率：平时 30 分钟一次（按钮比直播早 2 小时上线，余量够）；
        临近直播时缩到 10 分钟，免得刚好错过按钮上线那一段。
        """
        if not self.cfg.get("nav_auto", True):
            return
        now = _now()
        last = float(self.state.meta_get("nav_ts", 0) or 0)
        interval = 600.0 if self._should_poll_fast() else 1800.0
        if now - last < interval:
            return
        for game in self._games():
            try:
                found = await asyncio.to_thread(fetch_nav_act_id, game)
            except Exception as exc:  # noqa: BLE001
                logger.info(f"[miyo_codes] 读 {game} 的版区工具列表失败：{exc}")
                continue
            act = str((found or {}).get("act_id") or "")
            if not act or self.state.get_act(game) == act:
                continue
            self.state.set_act(game, act, "nav")
            off = float(found.get("offline_ts") or 0)
            tail = ""
            if off:
                tail = f"，按钮 {time.strftime('%m-%d %H:%M', time.localtime(off))} 下线"
            logger.info(
                f"[miyo_codes] 从版区工具列表拿到 {GAMES.get(game)} 的 act_id：{act}{tail}"
            )
        self.state.meta_set("nav_ts", now)

    async def _refresh_schedule(self) -> None:
        """读三家官方号的最新「前瞻预告」，拿到下一场直播时间。

        只解析「预告」帖：节目稿和回顾长图里的时间是兑换码失效时间，
        照抄会把"明晚 23:59"当成开播时间。

        失败必须退避重试：以前无论成败都写时间戳，网络抖一下就是 6 小时不再看，
        而那 6 小时很可能正好覆盖直播本身。
        """
        now = _now()
        nxt = float(self.state.meta_get("schedule_next_at", 0) or 0)
        if nxt and now < nxt:
            return
        failed = False
        for game in self._games():
            try:
                info = await asyncio.to_thread(fetch_notice, game)
            except Exception as exc:  # noqa: BLE001
                failed = True
                logger.info(f"[miyo_codes] 读 {game} 的前瞻预告失败：{exc}")
                continue
            if not info:
                failed = True
                continue
            # 只留还没过期太久的（直播已经过去 3 天的预告没意义）
            if float(info.get("live_at") or 0) < now - 3 * 86400:
                continue
            prev = self.state.get_schedule(game)
            if prev.get("post_id") != info.get("post_id"):
                logger.info(
                    f"[miyo_codes] {GAMES.get(game)} 下一场前瞻："
                    f"{info.get('version')} @ {info.get('live_at_text')}"
                )
            self.state.set_schedule(game, info)
        self.state.meta_set("schedule_ts", now)
        self.state.meta_set(
            "schedule_next_at", now + (1800 if failed else self._notice_interval())
        )

    async def _refresh_span(self) -> None:
        """每 6 小时对一次版本起止日期。

        主路径是**游戏内公告**：`X.Y版本更新说明` 的结束时间就是这一版的结束日，
        结构化、免登录、三家通用，还不用 OCR（星铁「4.7版本商店上新」在前瞻当晚
        20:55 就发了，直接写着 12-23）。
        起点用「前瞻帖公布的本版上线日」，所以只要公告能读到，周期当场就有。
        公告凑不齐时才退回 OCR 版本速览长图（只有星铁有）。
        """
        now = _now()
        nxt = float(self.state.meta_get("span_next_at", 0) or 0)
        if now < nxt:
            return
        failed = False
        for game in self._games():
            cached = self.state.meta_get(f"span:{game}") or {}
            rel = self.state.meta_get(f"release:{game}") or {}
            hint = float(rel.get("release_ts") or 0)
            ver_hint = str(rel.get("version") or "")
            try:
                span = await asyncio.to_thread(
                    fetch_version_span, game, cached, hint, ver_hint
                )
            except Exception as exc:  # noqa: BLE001
                failed = True
                logger.info(f"[miyo_codes] 读 {game} 的版本周期失败：{exc}")
                continue
            if not span:
                # 有来源能读却读不到，通常是被限流，半小时后再试
                if supports_version_span(game):
                    failed = True
                continue
            if span.get("post_id") != (cached or {}).get("post_id"):
                logger.info(
                    f"[miyo_codes] {GAMES.get(game)} 版本周期：{span.get('version')} "
                    f"{span.get('start_text')} ~ {span.get('end_text')}"
                    f"（{span.get('days')} 天，来源 {span.get('src') or '?'}）"
                )
            self.state.meta_set(f"span:{game}", span)
        self.state.meta_set("span_ts", now)
        self.state.meta_set("span_next_at", now + (1800 if failed else 6 * 3600))

    async def _maybe_remind(self, game: str) -> None:
        """知道有前瞻、但还没有 act_id → 提醒群里把直播间链接丢进来。

        分三段，各覆盖一类人：
        1. 距开播 3 天内先喊一次。预告一出来，直播间页面其实就已经存在了
           （4.7 的 act_id 里那个日期正好就是预告帖发布日），早点喊才来得及找；
        2. 开播前 remind_before_min 再喊一次，除非第 1 段刚喊过；
        3. 开播两小时后还没链接就最后催一次——不少人这时候才刷到群消息。
        """
        sched = self.state.get_schedule(game)
        live_at = float(sched.get("live_at") or 0)
        if not live_at:
            return
        now = _now()
        before = float(self.cfg.get("remind_before_min") or 0) * 60
        tag = sched.get("post_id") or sched.get("version")
        clock = time.strftime("%m-%d %H:%M", time.localtime(live_at))
        weekday = "周" + "一二三四五六日"[time.localtime(live_at).tm_wday]
        ver = f"{sched.get('version')} " if sched.get("version") else ""
        ask_word = GAME_ASK_WORD.get(game, "兑换")
        pre_key = f"preremind:{game}:{tag}"

        if before > 0 and now < live_at - before and live_at - now <= 3 * 86400:
            # 预告已出、离开播还有几小时到三天：先喊一次，别拖到最后一刻
            key = pre_key
            text = (
                f"📅 {clock}（{weekday}）是{GAMES.get(game)} {ver}前瞻直播。\n"
                "直播间页面现在就能打开了——谁把链接发到群里，我提前盯上，"
                "直播一结束就把兑换码发出来。"
            )
        elif before > 0 and live_at - before <= now <= live_at:
            if self.state.already_asked(pre_key):
                return  # 提前那次已经喊过，别在半小时里连着喊两遍
            key = f"remind:{game}:{tag}"
            text = (
                f"⏰ {clock}（{weekday}）是{GAMES.get(game)} {ver}前瞻直播。\n"
                "我还没有这场直播间的链接——谁把它发到群里（就是你平时打开看直播那个页面），"
                "我就能自动盯兑换码，直播一结束就发出来。"
            )
        elif live_at + 2 * 3600 <= now <= live_at + 4 * 3600:
            key = f"remind_after:{game}:{tag}"
            text = (
                f"👀 {GAMES.get(game)}{ver}前瞻直播已经开始一阵了，但我还没拿到直播间地址。\n"
                f"有人把链接发到群里，我马上取码；也可以直接说一句「{ask_word}兑换码」。"
            )
        else:
            return
        if self.state.already_asked(key):
            return
        sent_to = await self._broadcast(text)
        # 没发出去就别标记「已经提醒过」：目标群还没配好时标记了，
        # 等配置补上就再也不会提醒了
        if not sent_to:
            return
        self.state.mark_asked(key)
        logger.info(f"[miyo_codes] 已提醒 {game} 直播间链接（{clock}）")

    async def _check_game(self, game: str, act_id: str, source: str, force: bool = False) -> str:
        """检查一场直播，条件满足就发群。返回一句人话（给指令回显用）。"""
        info = await asyncio.to_thread(fetch_index, act_id)
        if info.get("game") and info["game"] != game:
            raise MiyoError(
                f"act_id 属于 {info['game']}，不是 {GAMES.get(game)}（配置串场了？）"
            )

        started = parse_dt(info.get("start", ""))
        if started and _now() - started > float(self.cfg.get("stale_hours") or 48) * 3600:
            return f"{GAMES.get(game)}：这场直播是 {info.get('start')} 的，已经不是当天，跳过"

        codes = await asyncio.to_thread(fetch_codes, act_id)
        if not codes:
            return f"{GAMES.get(game)}：接口里还没有码"

        # 只发已经在直播里揭晓的码，且要等直播结束——避免发半截
        revealed = [c for c in codes if c["to_get_time"] and c["to_get_time"] <= _now() + 60]
        if not revealed:
            return f"{GAMES.get(game)}：码还没揭晓"

        # 什么时候才允许发：直播结束 与 最后一个码揭晓 两者取晚，再压一个缓冲。
        # 为什么不能一到点就发：直播常常超时，最后那个码常常是在结束前后才刷出来；
        # 提前发出去就是一个少一个码的残缺消息，群里还得再补一次。
        end_ts = parse_dt(str(info.get("end") or ""))
        last_reveal = max(c["to_get_time"] for c in revealed)
        delay = float(self.cfg.get("post_delay_min") or 0) * 60
        ready_at = max(end_ts, last_reveal) + delay
        total = len(codes)
        # 三家的码数不一样：星铁/原神 3 个，绝区零一般就 1 个。
        # 所以门槛是"接口里有几个就等几个全部揭晓"，不能写死 3。
        need = total if self.cfg.get("require_all_codes", True) else min(
            int(self.cfg.get("min_codes") or 1), total
        )
        need = max(1, need)

        if not force:
            if len(revealed) < need:
                # 兜底：万一某个码接口就是不揭晓（改版、下架），别一直卡着不发
                grace = float(self.cfg.get("partial_grace_min") or 0) * 60
                if not (grace and _now() >= ready_at + grace):
                    return (
                        f"{GAMES.get(game)}：只刷出 {len(revealed)}/{total} 个码，等齐再发"
                    )
                logger.info(
                    f"[miyo_codes] {GAMES.get(game)} 超过 {int(grace // 60)} 分钟仍只有 "
                    f"{len(revealed)}/{total} 个码，先发已拿到的"
                )
            if ready_at > _now():
                return (
                    f"{GAMES.get(game)}：码齐了，但要等直播结束后再稳 "
                    f"{int(delay // 60)} 分钟（约 "
                    f"{time.strftime('%H:%M', time.localtime(ready_at))} 才发）"
                )

        sig = _hash_codes(revealed)
        # 从这里到发送结束必须串行。定时轮询和群里的指令可能同时走到这一步，
        # 两边都读到「还没发过」就会各广播一遍——群里看到两条一模一样的码，
        # 而且第二次还会覆盖状态文件里的记录。锁按 act_id 分，不同场次互不影响。
        async with self._lock_for(act_id):
            done = self.state.sent_targets(act_id, sig)
            all_keys = self._targets()
            if not all_keys:
                return "没配置发送对象（target_groups / target_friends 都是空的），先填群号或 QQ"
            if force:
                # 手动 /兑码：不管发没发过，重新发一遍
                targets = all_keys
            elif "*" in done:
                return f"{GAMES.get(game)}：这一场已经发过了（{len(revealed)} 个码）"
            else:
                # 只补发给还没收到的地方——新加一个群或一个人，能自动补上当前场次
                targets = [k for k in all_keys if k not in done]
                if not targets:
                    return f"{GAMES.get(game)}：这一场已经发过了（{len(revealed)} 个码）"

            expiry = ""
            release = ""
            release_ts = 0.0
            cur_ver = ""
            try:
                meta = await asyncio.to_thread(fetch_show_meta, game)
                expiry = str(meta.get("expiry") or "")
                release = str(meta.get("release_text") or "")
                release_ts = float(meta.get("release_ts") or 0)
                cur_ver = str(meta.get("version") or "")
                if release:
                    self.state.meta_set(
                        f"release:{game}",
                        {
                            "version": meta.get("version") or "",
                            "release_ts": meta.get("release_ts") or 0,
                            "release_text": release,
                            "src_act": act_id,
                        },
                    )
            except Exception:  # noqa: BLE001
                expiry = ""

            # 这一版哪天结束：来自游戏内公告（「X.Y版本更新说明」的结束时间）。
            # 必须跟本次直播公布的版本号对上才用，否则那是上一版的区间。
            span = None
            cached_span = self.state.meta_get(f"span:{game}") or {}
            if cached_span.get("end_ts"):
                same_ver = bool(
                    cur_ver
                    and cached_span.get("version")
                    and str(cached_span["version"]) == cur_ver
                )
                same_day = bool(
                    release_ts
                    and abs(float(cached_span.get("start_ts") or 0) - release_ts)
                    < 3 * 86400
                )
                if same_ver or same_day:
                    span = cached_span

            text = build_message(game, info, revealed, expiry, release, span)
            sent_to = await self._broadcast(text, targets)
            if sent_to:
                self.state.mark_sent(act_id, game, sig, revealed, sent_to)
            self.state.set_act(game, act_id, source or "auto")
            where = "、".join(self._key_label(k) for k in sent_to) or "没有可用的群/私聊"
            masked = "、".join(self._mask(k) for k in sent_to) or "没有可用的群/私聊"
            logger.info(
                f"[miyo_codes] {GAMES.get(game)} {info.get('title')} → 已发 {len(revealed)} 个码到 {masked}"
            )
            return f"{GAMES.get(game)}：已发送 {len(revealed)} 个码 → {where}"

    async def _broadcast(self, text: str, targets: list[str] | None = None) -> list[str]:
        """往群/私聊发一段话。targets 是 "g:群号" / "f:QQ号"，返回发成功的那些。"""
        ok: list[str] = []
        for key in targets if targets is not None else self._targets():
            umo = self._umo(key)
            if not umo:
                # 没有平台实例就没法发，提示一次比闷头刷异常有用
                logger.warning(
                    f"[miyo_codes] 没有可用平台实例，跳过 {self._mask(key)}；"
                    "请先在面板连上平台，或在插件配置里填 platform_id"
                )
                continue
            try:
                await self.context.send_message(umo, MessageChain([Plain(text)]))
                ok.append(key)
            except Exception as exc:  # noqa: BLE001
                # 常见原因：人没加机器人好友 / 群把我踢了 / 平台还没连上。
                # 只记日志，不重试——重试只会对着同一个错刷一晚上。
                logger.warning(f"[miyo_codes] 发到 {self._mask(key)} 失败：{exc}")
        return ok

    async def _ask_for_link(self, missing: list[str]) -> None:
        key = f"{_today()}:{','.join(sorted(missing))}"
        if self.state.already_asked(key):
            return
        names = "、".join(GAMES.get(g, g) for g in missing)
        text = (
            f"👀 今天是前瞻场次，我还没拿到{names}的直播间链接。\n"
            "谁把直播间链接发群里，我就能自动盯着兑换码，直播一结束就发出来。"
        )
        sent_to = await self._broadcast(text)
        if sent_to:
            self.state.mark_asked(key)

    def _can_force(self, event: AstrMessageEvent) -> bool:
        """能不能强制重发（无参数的 /兑码）。

        私聊里问的就是本人，放行；群里要求是群管理员，或者在 admin_users 名单里。
        名单这条路必须留着：event.is_admin() 读的是适配器填的 role，
        有的适配器（比如 SnowLuma）压根不填，只靠它群里永远没人能用。
        """
        try:
            if not event.get_group_id():
                return True
            if event.is_admin():
                return True
        except Exception:  # noqa: BLE001
            pass
        sender = str(event.get_sender_id() or "")
        admins = {str(x).strip() for x in (self.cfg.get("admin_users") or [])}
        return bool(sender and sender in admins)

    # ------------------------------------------------------------------ 指令
    @filter.command(CMD)
    async def cmd_codes(self, event: AstrMessageEvent):
        """兑码 / 兑码 <直播间链接或act_id> / 兑码状态"""
        raw = str(event.message_str or "").strip()
        arg = raw[len(CMD):].strip() if raw.startswith(CMD) else raw.strip()

        if arg in ("状态", "status"):
            # 群聊里打码，私聊里给全号：群成员没必要知道插件往哪些 QQ 发过码
            yield event.plain_result(self._status_text(masked=bool(event.get_group_id())))
            return

        act = parse_act_id(arg)
        if act:
            try:
                info = await asyncio.to_thread(fetch_index, act)
            except MiyoError as exc:
                yield event.plain_result(f"这个 act_id 不认：{exc}")
                return
            game = info.get("game") or ""
            if game not in GAMES:
                yield event.plain_result(
                    f"这场直播是「{info.get('title') or act}」，但我没对上是哪个游戏，先不发。"
                )
                return
            self.state.set_act(game, act, "link")
            try:
                result = await self._check_game(game, act, source="link")
            except MiyoError as exc:
                result = f"记住了，但取码失败：{exc}"
            yield event.plain_result(
                f"记住了：{info.get('title') or act}\n"
                f"直播时间 {info.get('start')} ~ {info.get('end')}\n"
                f"{result}"
            )
            return

        # 无参数 = 强制重发。这个动作会把三个游戏的码重新广播一遍，
        # 群里谁都能触发就等于装了个刷屏器，所以只放给管理员和私聊。
        if not self._can_force(event):
            yield event.plain_result(
                "强制重发要管理员才行。只想看状态发「/兑码 状态」；"
                "想知道某一款的码，直接说「崩铁兑换码」「原神兑换码」就好。"
            )
            return

        lines = []
        for game in self._games():
            aid = self._act_for(game)
            if not aid:
                lines.append(f"{GAMES.get(game)}：还没有 act_id（把直播间链接发群里就行）")
                continue
            try:
                lines.append(await self._check_game(game, aid, source="cmd", force=True))
            except MiyoError as exc:
                lines.append(f"{GAMES.get(game)}：{exc}")
            except Exception as exc:  # noqa: BLE001
                lines.append(f"{GAMES.get(game)}：出错 {exc}")
        yield event.plain_result("\n".join(lines))

    @filter.regex(ASK_CODES_RE)
    async def cmd_game_codes(self, event: AstrMessageEvent):
        """群里说「崩铁兑换码」，就把码回在当前会话。

        和 /兑码 的区别：这条**只回当前会话**，不广播到别的群——谁问就回给谁。
        也不等"直播结束"那道门禁，因为很多人是直播快结束才想起来问，
        那时候已经揭晓的码在游戏里就能换了，没必要让他们等到第二天。
        """
        if not self.cfg.get("ask_game_codes", True):
            return
        m = re.match(ASK_CODES_RE, str(event.message_str or ""), re.I)
        if not m:
            return
        word = m.group(1)
        game = GAME_ALIASES.get(word) or GAME_ALIASES.get(word.lower())
        if not game:
            return

        # 同一会话同一款 60 秒只回一次：不然有人连点几次，群里就是一串重复消息
        bucket = f"{event.get_group_id() or event.get_sender_id()}|{game}"
        now = _now()
        if now - float(self._ask_at.get(bucket) or 0) < 60:
            return
        self._ask_at[bucket] = now

        act = self._act_for(game)
        if not act:
            yield event.plain_result(
                f"我还没有{GAMES.get(game)}这一场的直播间地址。\n"
                "把直播间链接发到群里（就是你打开看直播那个页面），我立刻开始盯码。"
            )
            return
        try:
            info = await asyncio.to_thread(fetch_index, act)
            codes = await asyncio.to_thread(fetch_codes, act)
        except MiyoError as exc:
            yield event.plain_result(f"{GAMES.get(game)}：取码失败，{exc}")
            return
        if not codes:
            yield event.plain_result(
                f"{GAMES.get(game)}：接口里还没有码，等直播里放出来再问我"
            )
            return
        revealed = [c for c in codes if c["to_get_time"] and c["to_get_time"] <= _now() + 60]
        if not revealed:
            yield event.plain_result(f"{GAMES.get(game)}：码还没揭晓，直播里放出来我就能取")
            return

        expiry = ""
        release = ""
        try:
            meta = await asyncio.to_thread(fetch_show_meta, game)
            expiry = str(meta.get("expiry") or "")
            release = str(meta.get("release_text") or "")
        except Exception:  # noqa: BLE001
            expiry = ""
        yield event.plain_result(build_message(game, info, revealed, expiry, release, None))

    def _status_text(self, masked: bool = False) -> str:
        """把当前状态排成一段话。

        masked=True 时所有号码打码——这条指令能在群里执行，群里人不需要知道
        本插件往哪些 QQ 私聊发过码。私聊里问就给他看全的。
        """
        lines = ["【前瞻兑换码 · 状态】"]
        fmt_key = self._mask if masked else self._key_label
        fmt_num = self._mask_num if masked else str
        now = _now()
        for game in self._games():
            aid = self._act_for(game)
            src = "配置" if self._overrides().get(game) else ("群里接住" if aid else "—")
            lines.append(f"{GAMES.get(game)}：{aid or '未设置'}（{src}）")
            sched = self.state.get_schedule(game)
            live_at = float(sched.get("live_at") or 0)
            if live_at:
                tag = "已开播/已结束" if now > live_at else "下一场"
                ver = f"{sched.get('version')} " if sched.get("version") else ""
                lines.append(f"   {tag}：{ver}{sched.get('live_at_text')}")
            rel = self.state.meta_get(f"release:{game}") or {}
            if rel.get("release_text"):
                rv = f"{rel.get('version')} " if rel.get("version") else ""
                lines.append(f"   本版本上线：{rv}{rel.get('release_text')}")

            span = self.state.meta_get(f"span:{game}") or {}
            if span.get("end_ts") and span.get("days"):
                sv = f"{span.get('version')} " if span.get("version") else ""
                start_ts = float(span.get("start_ts") or 0)
                end_ts = float(span.get("end_ts") or 0)
                rng = (
                    f"{sv}{span.get('start_text')} ~ {span.get('end_text')}"
                    f"（{span.get('days')} 天）"
                )
                if start_ts <= now < end_ts:
                    lines.append(f"   本版周期：{rng} 进行中")
                elif start_ts > now:
                    lines.append(f"   下一版：{rng}")
                else:
                    lines.append(f"   上一版：{rng} 已结束")
                if end_ts > now:
                    # 前瞻一般在上线前 8~13 天。这里只作"大概什么时候盯"，
                    # 真日期还是等官方预告帖出来那一刻覆盖。
                    guess = end_ts - 12 * 86400
                    lines.append(
                        "   下一场前瞻预计："
                        f"{time.strftime('%m-%d', time.localtime(guess))} 前后（以预告为准）"
                    )
            elif rel.get("release_text"):
                lines.append("   版本周期：待新版上线后读游戏内公告")

        sent = self.state.data.get("sent") or {}
        if sent:
            lines.append("最近发过：")
            for act, rec in list(sent.items())[-3:]:
                t = time.strftime("%m-%d %H:%M", time.localtime(rec.get("ts", 0)))
                keys = rec.get("targets")
                if keys is None:
                    keys = [f"g:{g}" for g in (rec.get("groups") or [])] or ["（旧记录）"]
                where = "、".join(fmt_key(k) for k in keys)
                lines.append(f"  {rec.get('game')} {act} @ {t} → {where}")
        if self._last_error:
            lines.append(f"最近一次报错：{self._last_error}")
        groups = "、".join(fmt_num(g) for g in self._groups()) or "未配置"
        friends = "、".join(fmt_num(q) for q in self._friends()) or "未配置"
        lines.append(f"发到群：{groups}")
        lines.append(f"发到私聊：{friends}")
        gate = "全部码都揭晓" if self.cfg.get("require_all_codes", True) else (
            f"至少 {self.cfg.get('min_codes')} 个码"
        )
        lines.append(
            f"发码时机：直播结束 + {self.cfg.get('post_delay_min')} 分钟，且 {gate}"
        )
        return "\n".join(lines)

    # ------------------------------------------------------------------ 接住链接
    @filter.event_message_type(EventMessageType.ALL, priority=98)
    async def watch_live_link(self, event: AstrMessageEvent):
        """群里有人贴直播间链接 → 记下 act_id。不拦消息，不干扰聊天。"""
        try:
            gid = str(event.get_group_id() or "")
            if gid:
                if self._groups() and gid not in self._groups():
                    return
            else:
                # 私聊里贴的链接也认，但只认配置过的那几个 QQ，免得陌生人往里灌 act_id
                qq = str(event.get_sender_id() or "")
                if self._friends() and qq not in self._friends():
                    return
            act = parse_act_id(str(event.message_str or ""))
            if not act:
                return
            info = await asyncio.to_thread(fetch_index, act)
            game = info.get("game") or ""
            if game not in GAMES:
                return
            if self.state.get_act(game) == act:
                return
            self.state.set_act(game, act, "link")
            logger.info(f"[miyo_codes] 从群里接住 {game} 的 act_id={act}（{info.get('title')}）")
            if self.cfg.get("enabled", True):
                await self._check_game(game, act, source="link")
        except MiyoError as exc:
            logger.info(f"[miyo_codes] 群里的链接没认出来：{exc}")
        except Exception:  # noqa: BLE001
            logger.exception("[miyo_codes] 处理群里的链接时出错")

    async def terminate(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None
