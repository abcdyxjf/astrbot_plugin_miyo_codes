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
所以按优先级取：配置里手填 > 版区工具列表自动发现 > 群里接住的链接 > 上次用过的。
自动发现只在「直播兑换码」按钮上线的约 29 小时里有效；星铁已实测，另外两家待验证。
都不通的时候配置里手填最稳：把浏览器地址栏 ?act_id= 后面那串抄进来，10 秒的事。
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
    fetch_nav_survey,
    fetch_notice,
    fetch_show_meta,
    fetch_version_span,
    parse_act_id,
    parse_dt,
    reveal_codes,
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
    # 查兑换码的间隔（秒）：知道开播时间、且就在直播前后时用快的，其余用慢的。
    # 平时不靠这两个值驱动 —— 没到窗口的时候插件一个请求都不发
    "poll_interval_sec": 180,
    "slow_poll_interval_sec": 1800,
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

# 轮询间隔的硬下限（秒）。配置里填得比这更小也不生效，见 _interval_sec()。
# 公开插件必须留这条：接口不是官方 API，一个人把它改成几秒，
# 涨的是整个接口的压力，最后所有人的插件一起失效。
MIN_POLL_SEC = 60.0

# 每件事各自的节奏。_INF = 现在根本不用做这件事，一个请求都不发。
_INF = float("inf")
TICK_MAX = 3600.0  # 主循环最长睡 1 小时：醒来只重算，不发请求
FAIL_RETRY_SEC = 1800.0  # 单次失败后的退避上限，别让一次网络抖动变成长时间失明
NOTICE_WINDOW_SEC = 3600.0  # 进了前瞻窗口，1 小时读一次预告
NOTICE_BOOT_SEC = 6 * 3600.0  # 连版本周期都没有时的兜底间隔
SPAN_BOOT_SEC = 3600.0  # 还没有周期 / 版本已换掉：1 小时对一次
SPAN_NEAR_SEC = 6 * 3600.0  # 前瞻窗口内、直播前后
SPAN_FAR_SEC = 24 * 3600.0  # 平时：版本结束日一天最多变一次，日更足够
NAV_LIVE_SEC = 600.0  # 直播前后：按钮随时可能出现，10 分钟扫一次
NAV_WINDOW_SEC = 3600.0  # 前瞻窗口内但还不知道开播时间：1 小时扫一次
NAV_BOOT_SEC = 6 * 3600.0  # 什么都没有（新装/周期读不到）：慢慢探
FRESH_ACT_SEC = 4 * 3600.0  # act_id 刚拿到这么久以内，就当直播正开着
CODE_AFTER_SEC = 30 * 3600.0  # 直播结束后还留多久给补发（实测 14 小时仍可读码）
BLIND_SEC = 12 * 3600.0  # 「盲」状态（读不到任何周期）的探路间隔
WINDOW_LEAD_SEC = 15 * 86400.0  # 推算的前瞻窗口起点 = 版本结束日往前这么久

# 出厂预置的版本周期：2026-11-07 从三家游戏内公告读到。
#
# 为什么要有它：新装用户本地什么都没有，如果什么都不带，装上第一秒就得去问接口
# 才知道节奏。带上这一份之后，插件可以一直睡到「推算的前瞻窗口开始」那一刻，
# 期间一个请求都不发。
#
# 它只在「本地没有周期」且「还没过期」时使用：用户自己读到的永远优先；
# 过了版本结束日就完全不采用 —— 装载时立刻去读一次真实的，等于自带了
# 「安装时间晚于预置时间就补一次」的行为。这里写旧了也不会发错码，只会多读一次。
SEED_SPANS = {
    "hkrpg": {
        "version": "4.7",
        "start_ts": 1794351600,  # 11-11 07:00
        "end_ts": 1797976800,  # 12-23 06:00
        "start_text": "11-11",
        "end_text": "12-23",
        "days": 42,
    },
    "ys": {
        "version": "7.2",
        "start_ts": 1793746800,  # 11-04 07:00
        "end_ts": 1797372000,  # 12-16 06:00
        "start_text": "11-04",
        "end_text": "12-16",
        "days": 42,
    },
    "nap": {
        "version": "3.3",
        "start_ts": 1792533600,  # 10-21 06:00
        "end_ts": 1796162400,  # 12-02 06:00
        "start_text": "10-21",
        "end_text": "12-02",
        "days": 42,
    },
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


def _hash_codes(codes: list[dict]) -> str:
    body = "|".join(sorted(c["code"] for c in codes))
    return hashlib.sha1(body.encode("utf-8")).hexdigest()[:16]


# 接口明确回「这场已经结束了」的返回码。碰到一次就该记住，别再拿同一个
# act_id 反复去问：活动下线的同时码接口也关了，继续问只是白白打接口、
# 刷日志，还占着别人的限流额度。
_ACT_OVER_CODES = ("-500012", "-500007")


def _is_act_over(exc: BaseException) -> bool:
    """这个异常是不是在说「活动已结束」（而不是网络抖动、限流之类的临时故障）。"""
    text = str(exc)
    if any(code in text for code in _ACT_OVER_CODES):
        return True
    return "活动已结束" in text or "活动已下线" in text


# act_id 的来源，数字越大越可信。同一场次被多次写到时按这个比大小，
# 免得自动轮询的「auto」把版区工具列表发现的「nav」盖掉。
_SOURCE_RANK = {"nav": 4, "config": 3, "link": 2, "auto": 1, "test": 0}

# /兑码状态 里给人的说法
_SOURCE_LABEL = {
    "nav": "版区工具自动发现",
    "config": "配置手填",
    "link": "群里接住",
    "auto": "自动探测",
    "test": "测试",
}


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

    def get_act_rec(self, game: str) -> dict:
        """act_id 连同它是什么时候拿到的、从哪来的，一起返回。"""
        return dict(self.data["acts"].get(game) or {})

    def set_act(self, game: str, act_id: str, source: str) -> None:
        cur = self.data["acts"].get(game) or {}
        if cur.get("act_id") == act_id:
            # 同一场次只升不降。版区工具列表自动发现的（nav）是硬证据，
            # 但发送成功后还会走一次 set_act(source="auto")，不拦就会把
            # 「nav」改写成「auto」，状态里看着像配置里手工填的。
            old = str(cur.get("source") or "")
            if _SOURCE_RANK.get(old, 0) > _SOURCE_RANK.get(source, 0):
                source = old
        self.data["acts"][game] = {
            "act_id": act_id,
            "source": source,
            "ts": int(_now()),
        }
        self.save()

    # --- dead（已经结束的场次）---
    def is_act_dead(self, game: str, act_id: str) -> bool:
        """这场是不是已经被接口判定为结束了。"""
        rec = (self.data["meta"].get("dead") or {}).get(game) or {}
        return bool(act_id) and str(rec.get("act") or "") == str(act_id)

    def mark_act_dead(self, game: str, act_id: str, reason: str = "") -> bool:
        """记下「这场结束了」。返回 True 表示这是新标记的（用来只打一次日志）。

        只按 act_id 认人：下一场是新的 act_id，标记自然失效，不用手工清。
        """
        dead = self.data["meta"].setdefault("dead", {})
        if str((dead.get(game) or {}).get("act") or "") == str(act_id):
            return False
        dead[game] = {"act": str(act_id), "ts": int(_now()), "reason": str(reason)}
        self.save()
        return True

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
    "abcdyxjf",
    "原神 / 星穹铁道 / 绝区零 前瞻直播兑换码：官方接口取码，直播结束当天自动发群/私聊",
    "1.7.7",
    repo="https://github.com/abcdyxjf/astrbot_plugin_miyo_codes",
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
        self._seed_spans()
        self._task: asyncio.Task | None = None
        self._last_error: str = ""
        # 同一场次的发送必须串行：定时轮询和群里的指令可能同时进来，
        # 不加锁两边都会读到"还没发过"，群里就会收到两条一模一样的码
        self._locks: dict[str, asyncio.Lock] = {}
        # 「XX兑换码」指令的冷却，key = 群号|游戏，防同一个人连点刷屏
        self._ask_at: dict[str, float] = {}
        # 轮询下限的警告只打一次，避免用户不改配置就刷满日志
        self._poll_floor_warned: bool = False

        if cfg.get("enabled", True):
            coro = self._loop()
            try:
                self._task = asyncio.create_task(coro)
            except RuntimeError as exc:
                # 没有运行中的事件循环（离线自测）。不 close 会留一条 never awaited 警告
                coro.close()
                self._task = None
                # 这里是「插件加载成功但永不发码」的典型成因，必须留痕：
                # 以前静默吞掉，现象是日志里只有"加载插件"，之后再无任何动静。
                logger.warning(
                    f"[miyo_codes] 没有可用的事件循环，自动轮询没启动（{exc}）。"
                    " 在面板里重载一次这个插件即可恢复"
                )

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
            base = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "plugin_data"
            )
        return os.path.join(base, "state.json")

    def _seed_spans(self) -> None:
        """新装时先用出厂预置的版本周期，别一上来就去问接口。

        只在「本地没有这家周期」时补。过期的一概不填（安装时间已经晚于预置的
        版本结束日）—— 第一次巡检会立刻去读一次真实的，这就是「安装时间已经
        过了预置时间，就补一次请求」。
        """
        now = _now()
        for game, seed in SEED_SPANS.items():
            if game not in self._games():
                continue
            if self.state.meta_get(f"span:{game}"):
                continue  # 已经有了（自己读的或上次预置的），一律不动
            end_ts = float(seed.get("end_ts") or 0)
            if not end_ts or now >= end_ts:
                logger.debug(
                    f"[miyo_codes] 出厂预置的 {GAMES.get(game)} 周期已过期"
                    f"（到 {seed.get('end_text')}），装载后重新读一次"
                )
                continue
            self.state.meta_set(
                f"span:{game}", {**seed, "post_id": f"seed:{game}", "src": "seed"}
            )
            # 下一次核对定在「推算的前瞻窗口开始」那一刻（版本结束日往前 15 天）：
            # 在那之前一个请求都不用发；这个点已经过了就留空，第一轮立刻去读。
            due = end_ts - WINDOW_LEAD_SEC
            if due > now:
                self.state.meta_set(f"next:span:{game}", due)
                clock = time.strftime("%m-%d %H:%M", time.localtime(due))
            else:
                clock = "立刻"
            logger.info(
                f"[miyo_codes] 用出厂预置的 {GAMES.get(game)} 周期：{seed.get('version')} "
                f"{seed.get('start_text')} ~ {seed.get('end_text')}，下次核对 {clock}"
            )

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
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[miyo_codes] 读取平台元信息失败，跳过该实例：{exc}")
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
        act = override or self.state.get_act(game)
        # 已经结束的场次当没有：留着它只会让每一轮都去问一个已经不存在的活动
        if act and self.state.is_act_dead(game, act):
            return ""
        return act

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

    # ------------------------------------------------------------------ 节奏：各算各的
    def _live_at(self, game: str) -> float:
        """这一场预告里写的开播时间。0 = 还不知道。"""
        return float(self.state.get_schedule(game).get("live_at") or 0)

    def _span_end(self, game: str) -> float:
        """这一版哪天下线。0 = 还没读到。"""
        return float((self.state.meta_get(f"span:{game}") or {}).get("end_ts") or 0)

    def _near_live(self, game: str, now: float) -> bool:
        """现在是不是离这场直播很近（开播前半小时 ~ 结束后 6 小时）。"""
        live_at = self._live_at(game)
        return bool(live_at) and live_at - 1800 <= now <= live_at + 6 * 3600

    def _live_known_ahead(self, game: str, now: float) -> bool:
        """这场直播的时间已经知道了，而且还没过完。"""
        live_at = self._live_at(game)
        return bool(live_at) and now <= live_at + 6 * 3600

    def _live_soon(self, game: str, now: float) -> bool:
        """直播就在眼前（开播前 3 小时 ~ 结束后 4 小时）。

        只有这个区间里「求群友贴直播间链接」才不是误导 —— 以前是只要
        没有 act_id 就会喊，每天晚上都来一句「今天是前瞻场次」，纯属误报。
        """
        live_at = self._live_at(game)
        return bool(live_at) and live_at - 3 * 3600 <= now <= live_at + 4 * 3600

    def _preview_window(self, game: str, now: float) -> bool:
        """现在是不是落在这场版本的「前瞻快要来了」区间里。

        拿版本结束日往前推 12 天，当作下一场前瞻的大致日子，前后留几天余量。
        这个推算是粗的，只用来决定「什么时候开始查预告」，不参与任何
        「相信它就直接发码」的判断 —— 猜错了顶多多问两次，不会发错码。
        """
        end_ts = self._span_end(game)
        if not end_ts or now >= end_ts:
            return False
        guess = end_ts - 12 * 86400
        return end_ts - WINDOW_LEAD_SEC <= now <= guess + 5 * 86400

    def _act_alive(self, game: str, now: float) -> bool:
        """这场的 act_id 还在有效期内（没被判结束，且是 29 小时内拿到的）。

        版区那个「直播兑换码」按钮只活 29 小时，所以「刚拿到 act_id」本身
        就是窗口开着的硬信号；超过 29 小时的老 id 不算，否则一个怎么查都
        不报错的老 id 会把插件永久锁在高频里。
        """
        rec = self.state.get_act_rec(game)
        act = str(rec.get("act_id") or "")
        if not act or self.state.is_act_dead(game, act):
            return False
        return now - float(rec.get("ts") or 0) <= 29 * 3600

    def _blind(self, game: str) -> bool:
        """这家是不是「什么都不知道」：既没有版本周期，也没有本版版本号。

        正常情况下不该出现：三家公告里都写着「X.Y版本更新说明」的起止时间，
        周期直接就能读出来（原神/绝区零公告里通常只有一个版本，起点取那条公告
        自己的发布时间）。真落到这里，说明公告接口这会儿读不到，
        那就慢速探着，别按 1 小时去撞接口。（历史上这里出过一次误判：
        「只有一个版本的公告」原本算不出起点，导致原神/绝区零的周期永远读不到，
        被误当成「要等第一场直播才知道」。）
        """
        return not self._span_end(game) and not self.state.meta_get(f"release:{game}")

    def _notice_interval(self, game: str, now: float) -> float:
        """多久读一次这一家的前瞻预告。

        已经拿到这场的具体时间 → 彻底停手（等它开播就行，再查也是白问）；
        没进推算窗口 → 也不查；进了窗口才 1 小时一次。
        算不出窗口时慢速兜底，否则「没周期」就会变成「永远不查」。
        """
        if self._live_known_ahead(game, now):
            return _INF
        if self._preview_window(game, now):
            return NOTICE_WINDOW_SEC
        if not self._span_end(game):
            return BLIND_SEC if self._blind(game) else NOTICE_BOOT_SEC
        return _INF

    def _span_interval(self, game: str, now: float) -> float:
        """多久对一次这一版的起止日期。"""
        end = self._span_end(game)
        if not end:
            if self._blind(game):
                return BLIND_SEC  # 连版本号都没有：慢慢等第一场直播把版本号带回来
            return SPAN_BOOT_SEC  # 有版本号但周期没读到 / 版本刚换掉：1 小时补
        if now >= end:
            return SPAN_BOOT_SEC  # 版本已经换掉，赶紧读新版
        if self._preview_window(game, now) or self._near_live(game, now):
            return SPAN_NEAR_SEC
        return SPAN_FAR_SEC

    def _nav_interval(self, game: str, now: float) -> float:
        """多久扫一次这一家的版区工具列表（找「直播兑换码」按钮里的 act_id）。"""
        if not self.cfg.get("nav_auto", True):
            return _INF
        if self._act_alive(game, now):
            return _INF  # 这场的 act_id 已经拿到，不用再扫
        live_at = self._live_at(game)
        if live_at:
            # 按钮比开播早约 2 小时上线，提前 3 小时开始扫就够；之前一个请求都不发
            if live_at - 3 * 3600 <= now <= live_at + 6 * 3600:
                return NAV_LIVE_SEC
            return _INF
        if self._preview_window(game, now):
            return NAV_WINDOW_SEC
        if not self._span_end(game):
            # 周期都没有时，这条是唯一还可能自己发现直播的路子，不能停
            return BLIND_SEC if self._blind(game) else NAV_BOOT_SEC
        return _INF

    def _code_interval(self, game: str, now: float) -> float:
        """多久查一次这一场的兑换码。

        只有「这场直播前后」才值得查：
        开播前 3 小时开始慢速探着（万一码提前放出来），开播前半小时转快；
        结束后 6 小时内保持，再往后留一天给补发（实测结束后 14 小时仍能读全码）。
        平时一个请求都不发 —— 以前是只要手里有 act_id 就 3 分钟一问，
        提前几天贴进来的链接会白白刷上几天。
        """
        if not self._act_for(game):
            return _INF
        if self._sent_full(game):
            return self._slow_sec()  # 这一场已经完整发过：只等补码 / 补发新群
        live_at = self._live_at(game)
        if not live_at:
            # 从版区按钮拿到 act_id、还不知道开播时间：刚拿到那几小时按快的走
            rec = self.state.get_act_rec(game)
            age = now - float(rec.get("ts") or 0)
            return self._fast_sec() if age <= FRESH_ACT_SEC else self._slow_sec()
        if self._near_live(game, now):
            return self._fast_sec()
        if live_at - 3 * 3600 <= now <= live_at + CODE_AFTER_SEC:
            return self._slow_sec()
        return _INF

    def _sent_full(self, game: str) -> bool:
        """这一场是不是已经完整发出去了（码齐、发给了所有对象）。"""
        return bool(self.state.meta_get(f"full:{self._act_for(game)}", 0))

    def _due(self, kind: str, game: str, now: float, interval: float) -> bool:
        """这件事到这个游戏的点了没有。interval=inf 时永远不算到点。"""
        if interval == _INF:
            return False
        return now >= float(self.state.meta_get(f"next:{kind}:{game}", 0) or 0)

    def _postpone(
        self, kind: str, game: str, now: float, interval: float, ok: bool = True
    ) -> None:
        """记下这件事在这家下次什么时候再做。

        失败时把间隔压到 30 分钟以内，别让一次网络抖动就让这一家「失明」一整天。
        """
        if interval == _INF:
            return
        step = interval if ok else min(interval, FAIL_RETRY_SEC)
        self.state.meta_set(f"next:{kind}:{game}", now + step)

    def _lock_for(self, act_id: str) -> asyncio.Lock:
        """同一场次共用一把锁。不同场次、不同游戏互不影响。"""
        lock = self._locks.get(act_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[act_id] = lock
        return lock

    # ------------------------------------------------------------------ 主循环
    def _interval_sec(self, key: str, default: float) -> float:
        """读一个秒数配置。低于下限的一律按下限执行。

        为什么要设下限：这个插件是公开的，接口又不是官方 API。一个人把它改成
        5 秒，看着只是他自己在刷，实际是整个接口的压力上去了 —— 一旦官方加上
        验证或限流，所有装了这个插件的人（包括配置正常的人）一起失效。
        所以这里不信任配置值，兜一层底。
        """
        try:
            val = float(self.cfg.get(key))
        except (TypeError, ValueError):
            # 没填 / 填了空串 / 填了乱码 → 用默认。注意不能写 `raw or default`：
            # 那会把合法的 0 也当成"没填"，0 秒就绕过下限了
            val = default
        if val < MIN_POLL_SEC:
            # 只警告一次，否则用户不改就会一直刷日志
            if not self._poll_floor_warned:
                self._poll_floor_warned = True
                logger.warning(
                    f"[miyo_codes] {key} = {val:g} 秒低于下限，已按 {MIN_POLL_SEC:g} 秒执行。"
                    " 调太快会被官方接口限流，连带影响所有用这个插件的人。"
                )
            return MIN_POLL_SEC
        return val

    def _fast_sec(self) -> float:
        return self._interval_sec("poll_interval_sec", 180.0)

    def _slow_sec(self) -> float:
        return self._interval_sec("slow_poll_interval_sec", 1800.0)

    def _next_wait(self) -> float:
        """主循环该睡多久：所有排着队的事里最近的那个时刻。

        三个游戏各算各的 —— 谁有活干就按谁的节奏叫醒，没活干的那家
        一个请求都不会发出去，也不会因为别家进了直播就被拖着一起高频。
        全部闲着时睡满 1 小时（醒来只重算，不发请求）。
        """
        now = _now()
        due: list[float] = []
        for game in self._games():
            for kind, interval in (
                ("span", self._span_interval(game, now)),
                ("notice", self._notice_interval(game, now)),
                ("nav", self._nav_interval(game, now)),
                ("code", self._code_interval(game, now)),
            ):
                if interval == _INF:
                    continue
                due.append(float(self.state.meta_get(f"next:{kind}:{game}", 0) or 0))
            live_at = self._live_at(game)
            if live_at:
                # 几个必须准点醒来的时刻：开始提醒、开始扫按钮、切高频、切回慢速
                for edge in (
                    live_at - 3 * 86400,
                    live_at - 3 * 3600,
                    live_at - 1800,
                    live_at + 2 * 3600,
                    live_at + 6 * 3600,
                ):
                    if edge > now:
                        due.append(edge)
        if not due:
            return TICK_MAX
        return max(MIN_POLL_SEC, min(min(due) - now, TICK_MAX))

    async def _loop(self) -> None:
        await asyncio.sleep(10)  # 等平台连上，别在启动瞬间抢发
        # 心跳日志：有了这一行，"轮询到底起没起来"在日志里就能直接看出来。
        # 之前没有它，任务是没建起来、还是中途静默死掉，日志里一点线索都没有。
        names = "、".join(GAMES.get(g, g) for g in self._games())
        logger.info(f"[miyo_codes] 自动轮询已启动：{names}")
        while True:
            # 先干活、再算睡多久：这样「下一次该做什么」是根据这一轮之后的状态算的，
            # 不会有刚干完又被叫醒一趟的浪费。
            try:
                await self._tick()
            except Exception as exc:  # noqa: BLE001
                logger.exception("[miyo_codes] 轮询出错")
                self._last_error = str(exc)
            try:
                wait = self._next_wait()
            except Exception:  # noqa: BLE001
                # 连"该睡多久"都算不出来时，别让整个任务静默死掉：退成慢速继续跑
                logger.exception("[miyo_codes] 计算下次时间失败，先按 30 分钟继续")
                wait = 1800.0
            await asyncio.sleep(wait)

    async def _tick(self) -> None:
        """一次巡检。三件事、三个游戏各按各的节奏走，互不牵连。"""
        if not self.cfg.get("enabled", True):
            return
        now = _now()
        for game in self._games():
            await self._check_span(game, now)
        for game in self._games():
            await self._check_notice(game, now)
        for game in self._games():
            await self._scan_nav(game, now)
        missing: list[str] = []
        for game in self._games():
            act = self._act_for(game)
            if not act:
                missing.append(game)
                await self._maybe_remind(game)
                continue
            interval = self._code_interval(game, now)
            if not self._due("code", game, now, interval):
                continue
            self._postpone("code", game, now, interval)
            try:
                await self._check_game(game, act, source="auto")
            except MiyoError as exc:
                self._last_error = f"{game}: {exc}"
                if _is_act_over(exc):
                    # 结束了就记下来，这场再也不用查；下一场拿到新 act_id 自动恢复
                    if self.state.mark_act_dead(game, act, str(exc)):
                        logger.info(
                            f"[miyo_codes] {game} 这场已经结束（{exc}），"
                            "不再轮询它，等下次前瞻拿到新的 act_id 会自动接着盯"
                        )
                else:
                    logger.info(f"[miyo_codes] {game} 暂时没结果：{exc}")
            except Exception as exc:  # noqa: BLE001
                self._last_error = f"{game}: {exc}"
                logger.exception(f"[miyo_codes] {game} 检查失败")
        # 求链接只在「直播就在眼前」时才发：别的日子喊「今天是前瞻场次」是误报
        soon = [g for g in missing if self._live_soon(g, now)]
        if soon and self.cfg.get("ask_for_link"):
            await self._ask_for_link(soon)

    async def _check_span(self, game: str, now: float) -> None:
        """对一次这一版的起止日期（游戏内公告优先，读不到就沿用上次）。

        主路径是**游戏内公告**：`X.Y版本更新说明` 的结束时间就是这一版的结束日，
        结构化、免登录、三家通用，还不用 OCR（星铁「4.7版本商店上新」在前瞻当晚
        20:55 就发了，直接写着 12-23）。
        起点用「前瞻帖公布的本版上线日」，所以只要公告能读到，周期当场就有。
        公告凑不齐时才退回 OCR 版本速览长图（只有星铁有）。

        读到 None 不算失败：那通常只是「公告还没切到这一版」，下次按间隔再来。
        以前把它当失败、每次退避半小时，白白多问了几十倍。
        """
        interval = self._span_interval(game, now)
        if not self._due("span", game, now, interval):
            return
        cached = self.state.meta_get(f"span:{game}") or {}
        rel = self.state.meta_get(f"release:{game}") or {}
        span = None
        try:
            span = await asyncio.to_thread(
                fetch_version_span,
                game,
                cached,
                float(rel.get("release_ts") or 0),
                str(rel.get("version") or ""),
            )
        except Exception as exc:  # noqa: BLE001
            logger.info(f"[miyo_codes] 读 {GAMES.get(game)} 的版本周期失败：{exc}")
        if span:
            if span.get("post_id") != cached.get("post_id"):
                logger.info(
                    f"[miyo_codes] {GAMES.get(game)} 版本周期：{span.get('version')} "
                    f"{span.get('start_text')} ~ {span.get('end_text')}"
                    f"（{span.get('days')} 天，来源 {span.get('src') or '?'}）"
                )
            self.state.meta_set(f"span:{game}", span)
        self._postpone("span", game, now, interval)

    async def _check_notice(self, game: str, now: float) -> None:
        """读一次这一家的前瞻预告。拿到开播时间就停手（见 _notice_interval）。

        只解析「预告」帖：节目稿和回顾长图里的时间是兑换码失效时间，
        照抄会把「明晚 23:59」当成开播时间。

        网络出错才退避重试；「还没发预告」是正常状态，不算失败。
        """
        interval = self._notice_interval(game, now)
        if not self._due("notice", game, now, interval):
            return
        try:
            info = await asyncio.to_thread(fetch_notice, game)
        except Exception as exc:  # noqa: BLE001
            logger.info(f"[miyo_codes] 读 {GAMES.get(game)} 的前瞻预告失败：{exc}")
            self._postpone("notice", game, now, interval, ok=False)
            return
        # 只留还没过期太久的（直播已经过去 3 天的预告没意义）
        if info and float(info.get("live_at") or 0) >= now - 3 * 86400:
            prev = self.state.get_schedule(game)
            if prev.get("post_id") != info.get("post_id"):
                logger.info(
                    f"[miyo_codes] {GAMES.get(game)} 下一场前瞻："
                    f"{info.get('version')} @ {info.get('live_at_text')}"
                )
            self.state.set_schedule(game, info)
        self._postpone("notice", game, now, interval)

    async def _scan_nav(self, game: str, now: float) -> None:
        """扫一次这一家的版区工具列表，从「直播兑换码」按钮的链接里取 act_id。

        这是目前唯一不用人给、也不用登录的来源。按钮只在直播前后约 29 小时
        存在，所以平时扫不到是正常的 —— 不能因为扫不到就当成失败。
        这一场的 act_id 已经拿到就不再扫（见 _nav_interval）。
        """
        interval = self._nav_interval(game, now)
        if not self._due("nav", game, now, interval):
            return
        self._postpone("nav", game, now, interval)
        try:
            survey = await asyncio.to_thread(fetch_nav_survey, game)
        except Exception as exc:  # noqa: BLE001
            logger.info(f"[miyo_codes] 读 {GAMES.get(game)} 的版区工具列表失败：{exc}")
            return
        # 把"这次到底扫到了什么"记下来：按钮只在窗口期存在，扫不到是常态，
        # 只有记住扫到的工具名，下次窗口过了才能分清「还没开」和「真没有」。
        seen = dict(self.state.meta_get("nav_seen", {}) or {})
        seen[game] = {
            "ts": int(now),
            "clients": {k: len(v) for k, v in (survey.get("clients") or {}).items()},
            "names": {
                k: list(v)[:12] for k, v in (survey.get("clients") or {}).items()
            },
            "act_id": str(survey.get("act_id") or ""),
        }
        self.state.meta_set("nav_seen", seen)
        act = str(survey.get("act_id") or "")
        if not act:
            return
        cur = self.state.get_act_rec(game)
        # 已经知道且来源也是 nav 就不用再写；来源是别的（比如贴链接进来的）
        # 顺手补正，省得 /兑码状态 里一直显示旧的来源
        if cur.get("act_id") == act and cur.get("source") == "nav":
            return
        self.state.set_act(game, act, "nav")
        off = float(survey.get("offline_ts") or 0)
        tail = ""
        if off:
            tail = f"，按钮 {time.strftime('%m-%d %H:%M', time.localtime(off))} 下线"
        logger.info(
            f"[miyo_codes] 从版区工具列表拿到 {GAMES.get(game)} 的 act_id：{act}{tail}"
        )

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

    async def _check_game(
        self, game: str, act_id: str, source: str, force: bool = False
    ) -> str:
        """检查一场直播，条件满足就发群。返回一句人话（给指令回显用）。"""
        info = await asyncio.to_thread(fetch_index, act_id)
        if info.get("game") and info["game"] != game:
            raise MiyoError(
                f"act_id 属于 {info['game']}，不是 {GAMES.get(game)}（配置串场了？）"
            )

        codes = await asyncio.to_thread(fetch_codes, act_id)
        if not codes:
            return f"{GAMES.get(game)}：接口里还没有码"

        # 这场是不是早就结束了。
        # 以前这里是 `if started and ...`，start 一旦格式变了就等于没检查——
        # 插件重启丢了状态文件之后，会把几天前那场的过期码重新发进群。
        stale_limit = float(self.cfg.get("stale_hours") or 48) * 3600
        started = parse_dt(info.get("start", ""))
        if not started:
            # start 读不出来，拿码的揭晓时间当替补（它通常就是直播当天）
            started = max(
                (c["to_get_time"] for c in codes if c.get("to_get_time")), default=0.0
            )
        if started and _now() - started > stale_limit:
            return f"{GAMES.get(game)}：这场直播是 {info.get('start') or '更早'} 的，已经过期，跳过"
        if not started and not force:
            # 开始时间和揭晓时间都读不出来，就没法判断这场是新的还是旧的。
            # 自动轮询宁可跳过（大不了手动 /兑码 再发），也不能往群里灌过期码。
            logger.warning(
                f"[miyo_codes] {GAMES.get(game)} {act_id} 拿不到任何时间字段，跳过自动发送"
            )
            return f"{GAMES.get(game)}：这场的时间读不出来，自动跳过（可以用 /兑码 手动发）"

        # 只发已经在直播里揭晓的码，且要等直播结束——避免发半截
        revealed = reveal_codes(codes, info, _now())
        if not revealed:
            return f"{GAMES.get(game)}：码还没揭晓"

        # 什么时候才允许发：直播结束 与 最后一个码揭晓 两者取晚，再压一个缓冲。
        # 为什么不能一到点就发：直播常常超时，最后那个码常常是在结束前后才刷出来；
        # 提前发出去就是一个少一个码的残缺消息，群里还得再补一次。
        end_ts = parse_dt(str(info.get("end") or ""))
        last_reveal = max((c["to_get_time"] for c in revealed), default=0.0)
        delay = float(self.cfg.get("post_delay_min") or 0) * 60
        ready_at = max(end_ts, last_reveal) + delay
        total = len(codes)
        # 三家的码数不一样：星铁/原神 3 个，绝区零一般就 1 个。
        # 所以门槛是"接口里有几个就等几个全部揭晓"，不能写死 3。
        need = (
            total
            if self.cfg.get("require_all_codes", True)
            else min(int(self.cfg.get("min_codes") or 1), total)
        )
        need = max(1, need)

        if not force:
            if len(revealed) < need:
                # 兜底：万一某个码接口就是不揭晓（改版、下架），别一直卡着不发
                grace = float(self.cfg.get("partial_grace_min") or 0) * 60
                if not (grace and _now() >= ready_at + grace):
                    return f"{GAMES.get(game)}：只刷出 {len(revealed)}/{total} 个码，等齐再发"
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
                    return (
                        f"{GAMES.get(game)}：这一场已经发过了（{len(revealed)} 个码）"
                    )

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
                if len(revealed) >= need:
                    # 码齐了、也发出去了：这一场之后降成慢速，只等接口补码或新群补发。
                    # 不降速的话，直播结束后那几个小时还会每 3 分钟问一次。
                    self.state.meta_set(f"full:{act_id}", int(_now()))
            self.state.set_act(game, act_id, source or "auto")
            where = (
                "、".join(self._key_label(k) for k in sent_to) or "没有可用的群/私聊"
            )
            masked = "、".join(self._mask(k) for k in sent_to) or "没有可用的群/私聊"
            logger.info(
                f"[miyo_codes] {GAMES.get(game)} {info.get('title')} → 已发 {len(revealed)} 个码到 {masked}"
            )
            return f"{GAMES.get(game)}：已发送 {len(revealed)} 个码 → {where}"

    async def _broadcast(
        self, text: str, targets: list[str] | None = None
    ) -> list[str]:
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
        except Exception as exc:  # noqa: BLE001
            # 取不到身份信息就按「不是管理员」处理，宁可不给权限
            logger.debug(f"[miyo_codes] 判断管理员身份失败，按非管理员处理：{exc}")
        sender = str(event.get_sender_id() or "")
        admins = {str(x).strip() for x in (self.cfg.get("admin_users") or [])}
        return bool(sender and sender in admins)

    # ------------------------------------------------------------------ 指令
    @filter.command(CMD)
    async def cmd_codes(self, event: AstrMessageEvent):
        """兑码 / 兑码 <直播间链接或act_id> / 兑码状态"""
        raw = str(event.message_str or "").strip()
        arg = raw[len(CMD) :].strip() if raw.startswith(CMD) else raw.strip()

        if arg in ("状态", "status"):
            # 群聊里打码，私聊里给全号：群成员没必要知道插件往哪些 QQ 发过码
            yield event.plain_result(
                self._status_text(masked=bool(event.get_group_id()))
            )
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
                lines.append(
                    f"{GAMES.get(game)}：还没有 act_id（把直播间链接发群里就行）"
                )
                continue
            try:
                lines.append(
                    await self._check_game(game, aid, source="cmd", force=True)
                )
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
        m = re.match(ASK_CODES_RE, str(event.message_str or ""), re.IGNORECASE)
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
        revealed = reveal_codes(codes, info, _now())
        if not revealed:
            yield event.plain_result(
                f"{GAMES.get(game)}：码还没揭晓，直播里放出来我就能取"
            )
            return

        expiry = ""
        release = ""
        try:
            meta = await asyncio.to_thread(fetch_show_meta, game)
            expiry = str(meta.get("expiry") or "")
            release = str(meta.get("release_text") or "")
        except Exception:  # noqa: BLE001
            expiry = ""
        yield event.plain_result(
            build_message(game, info, revealed, expiry, release, None)
        )

    def _pace_text(self, game: str, now: float) -> str:
        """这一家下一步什么时候做什么。给 /兑码状态 用。

        「平时不触发」这件事在日志里是看不出来的（没有请求就没有日志），
        所以把它摊在状态里：窗口外会明确写「不查」，一眼就知道它在睡觉。
        """

        def when(kind: str) -> str:
            gap = float(self.state.meta_get(f"next:{kind}:{game}", 0) or 0) - now
            if gap <= 0:
                return "马上"
            if gap < 3600:
                return f"{int(gap // 60)} 分钟后"
            if gap < 48 * 3600:
                return f"{gap / 3600:.1f} 小时后"
            return f"{int(gap // 86400)} 天后"

        if self._notice_interval(game, now) == _INF:
            notice = (
                "已拿到，等开播" if self._live_known_ahead(game, now) else "窗口外不查"
            )
        else:
            notice = when("notice")
        if self._nav_interval(game, now) == _INF:
            nav = "已拿到 act_id" if self._act_alive(game, now) else "暂不扫"
        else:
            nav = when("nav")
        code_iv = self._code_interval(game, now)
        code = "无" if code_iv == _INF else f"每 {int(code_iv // 60)} 分钟"
        return f"   节奏：预告 {notice}｜版区 {nav}｜查码 {code}｜版本 {when('span')}"

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
            src = (
                "配置" if self._overrides().get(game) else ("群里接住" if aid else "—")
            )
            # 有记录就按记录的来源说，别一律写成"群里接住"——
            # 自动发现的 act_id 和人工贴链接进来的，排查时是要分开看的
            rec = (self.state.data.get("acts") or {}).get(game) or {}
            if aid and rec.get("act_id") == aid:
                src = _SOURCE_LABEL.get(str(rec.get("source") or ""), src)
            if aid:
                lines.append(f"{GAMES.get(game)}：{aid}（{src}）")
            elif rec.get("act_id"):
                # 记着但已被判结束：显式写出来，免得看着像「从来没拿到过码」
                lines.append(
                    f"{GAMES.get(game)}：{rec.get('act_id')}（已结束，不再查）"
                )
            else:
                lines.append(f"{GAMES.get(game)}：未设置（{src}）")
            nav = (self.state.meta_get("nav_seen", {}) or {}).get(game) or {}
            if nav.get("ts"):
                cnt = nav.get("clients") or {}
                detail = (
                    " / ".join(
                        f"{'网页' if k == 'web' else '安卓'} {v} 项"
                        for k, v in cnt.items()
                    )
                    or "读不到"
                )
                when = time.strftime("%m-%d %H:%M", time.localtime(float(nav["ts"])))
                if nav.get("act_id"):
                    lines.append(
                        f"   版区工具：{detail}，有「直播兑换码」（{when} 扫）"
                    )
                else:
                    lines.append(
                        f"   版区工具：{detail}，无「直播兑换码」"
                        f"（{when} 扫，窗口外正常）"
                    )
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
                # 出厂预置的标一下：用户看到就知道这份还是随包带来的，没联网核对过
                tag = "（出厂预置）" if span.get("src") == "seed" else ""
                if start_ts <= now < end_ts:
                    lines.append(f"   本版周期：{rng} 进行中{tag}")
                elif start_ts > now:
                    lines.append(f"   下一版：{rng}{tag}")
                else:
                    lines.append(f"   上一版：{rng} 已结束{tag}")
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
            lines.append(self._pace_text(game, now))

        sent = self.state.data.get("sent") or {}
        if sent:
            lines.append("最近发过：")
            for act, rec in list(sent.items())[-3:]:
                t = time.strftime("%m-%d %H:%M", time.localtime(rec.get("ts", 0)))
                keys = rec.get("targets")
                if keys is None:
                    keys = [f"g:{g}" for g in (rec.get("groups") or [])] or [
                        "（旧记录）"
                    ]
                where = "、".join(fmt_key(k) for k in keys)
                lines.append(f"  {rec.get('game')} {act} @ {t} → {where}")
        if self._last_error:
            lines.append(f"最近一次报错：{self._last_error}")
        groups = "、".join(fmt_num(g) for g in self._groups()) or "未配置"
        friends = "、".join(fmt_num(q) for q in self._friends()) or "未配置"
        lines.append(f"发到群：{groups}")
        lines.append(f"发到私聊：{friends}")
        gate = (
            "全部码都揭晓"
            if self.cfg.get("require_all_codes", True)
            else (f"至少 {self.cfg.get('min_codes')} 个码")
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
            logger.info(
                f"[miyo_codes] 从群里接住 {game} 的 act_id={act}（{info.get('title')}）"
            )
            if self.cfg.get("enabled", True):
                await self._check_game(game, act, source="link")
        except MiyoError as exc:
            logger.info(f"[miyo_codes] 群里的链接没认出来：{exc}")
        except Exception:  # noqa: BLE001
            logger.exception("[miyo_codes] 处理群里的链接时出错")

    async def terminate(self) -> None:
        """卸载/重载时停掉后台任务。

        要 await 一下：只 cancel 不等待的话，重载时容易在日志里留一条
        「Task was destroyed but it is pending」——看着像出错，其实是没等它退出。
        """
        task, self._task = self._task, None
        if not task or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            logger.exception("[miyo_codes] 停止自动轮询时出错")
