"""米哈游前瞻直播兑换码：取数 + 排版。

数据源是米游社直播间自己在用的那套接口，公开、免登录、返回结构化数据：

    GET https://api-takumi.mihoyo.com/event/miyolive/index?act_id=<场次ID>
        → 直播信息（标题 / 开始 / 结束 / 是否结束 / code_ver）

    GET https://api-takumi.mihoyo.com/event/miyolive/refreshCode?act_id=<场次ID>&time=<秒>
        → code_list，每个码带 to_get_time（该码在直播里"揭晓"的时间戳）

为什么用这个而不是爬帖子：帖子里的码是玩家转述的，可能错、
可能是国际服、可能漏。这个接口是官方给直播间自己用的，码本身不会错。

关于 act_id
----------
每一场前瞻有一个 act_id，形如 ea202610261626345889。
没有"列出所有前瞻"的接口，所以 act_id 只能从外面来，按优先级：
配置手填 > 群里有人贴直播间链接 > 米游社版区工具列表里的「直播兑换码」。

最后那条见 fetch_nav_act_id：按钮只在前瞻前后约 29 小时里出现，
出现时它的跳转链接就带着 act_id。这是唯一不用人给、也不用登录的来源。
"""

from __future__ import annotations

import html
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from astrbot.api import logger

API_BASE = "https://api-takumi.mihoyo.com/event/miyolive/"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://webstatic.mihoyo.com/",
    "Accept": "application/json, text/plain, */*",
}

# game_biz → 展示名。键名跟米游社接口里的 data.game 对齐
GAMES = {
    "hkrpg": "崩坏：星穹铁道",
    "ys": "原神",
    "nap": "绝区零",
}

# 三家官方号在米游社的 uid，用来读「前瞻特别节目预告」和「兑换码失效时间」
# 全部实测过（2026-10-30）：原神 75276539 / 星穹铁道 288909600 / 绝区零 152039148
OFFICIAL_UID = {
    "hkrpg": "288909600",
    "ys": "75276539",
    "nap": "152039148",
}

# 米游社版块 id（搜索帖子时要带上，否则搜到别的区去了）
GAME_GIDS = {"hkrpg": 6, "ys": 2, "nap": 8}


class MiyoError(Exception):
    """接口返回 retcode != 0，或结构不是预期的样子。"""


def _get(path: str, timeout: float = 15.0) -> dict:
    url = API_BASE + path
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.URLError as exc:
        raise MiyoError(f"网络不通：{exc}") from exc

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MiyoError(f"返回不是 JSON：{raw[:120]}") from exc

    if payload.get("retcode") != 0:
        raise MiyoError(
            f"接口拒绝：retcode={payload.get('retcode')} {payload.get('message')}"
        )
    return payload.get("data") or {}


def fetch_index(act_id: str) -> dict:
    """直播场次信息。返回 {title, start, end, is_end, game, code_ver, ...}"""
    data = _get(f"index?act_id={act_id}")
    live = data.get("live") or {}
    if not live:
        raise MiyoError("这场直播没有 live 数据（act_id 可能不对）")
    return {
        "act_id": act_id,
        "title": str(live.get("title") or ""),
        "start": str(live.get("start") or ""),
        "end": str(live.get("end") or ""),
        "is_end": bool(live.get("is_end")),
        "code_ver": str(live.get("code_ver") or ""),
        "game": str(data.get("game") or ""),
        "now": int(live.get("now") or time.time()),
    }


def fetch_codes(act_id: str) -> list[dict]:
    """兑换码列表。to_get_time 之前那是"还没揭晓"，不要提前发。"""
    data = _get(f"refreshCode?act_id={act_id}&time={int(time.time())}")
    out = []
    for item in data.get("code_list") or []:
        code = str(item.get("code") or "").strip()
        if not code:
            continue
        # 接口平时给的是数字，但不保证哪天变成字符串或干脆缺字段——
        # 这里一旦抛异常，整场都取不出来，所以单独兜一下。
        try:
            to_get = int(item.get("to_get_time") or 0)
        except (TypeError, ValueError):
            to_get = 0
        out.append(
            {
                "code": code,
                "reward": strip_tags(str(item.get("title") or "")),
                "to_get_time": to_get,
            }
        )
    return out


_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_tags(text: str) -> str:
    """<p>星琼<span>×100</span>，信用点<span>×50000</span></p> → 星琼×100，信用点×50000"""
    plain = html.unescape(_TAG_RE.sub("", text or ""))
    return _WS_RE.sub(" ", plain).strip()


_ACT_IN_URL_RE = re.compile(r"act_id=([A-Za-z0-9]{6,40})", re.IGNORECASE)
_ACT_BARE_RE = re.compile(r"\b(ea\d{16,22})\b", re.IGNORECASE)


def parse_act_id(text: str) -> str:
    """从一段话里抠出 act_id。群里贴的直播间链接、手打的 ID 都能认。"""
    if not text:
        return ""
    m = _ACT_IN_URL_RE.search(text) or _ACT_BARE_RE.search(text)
    return m.group(1) if m else ""


def parse_dt(text: str) -> float:
    """ "2026-10-30 19:10:00" → 时间戳。解析不了就返回 0。"""
    text = (text or "").strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
        try:
            return time.mktime(time.strptime(text, fmt))
        except ValueError:
            continue
    return 0.0


def reveal_codes(codes: list[dict], info: dict, now: float | None = None) -> list[dict]:
    """这几个码现在能不能发。

    正常路径：to_get_time 到了就算揭晓（直播里念过一遍，公开了）。
    失效路径：接口哪天不再带 to_get_time（全 0），那么"按揭晓时间过滤"会让
    revealed 永远是空的 —— 插件从此一声不响，而且不报错。这是最糟的失效方式：
    静默。所以退一步，改用「直播是否已经结束」来放行：结束了就认为码都念完了，
    没结束仍然不发（别把还没念的码提前漏出去）。
    """
    now = time.time() if now is None else now
    if any(c.get("to_get_time") for c in codes):
        return [
            c for c in codes if c.get("to_get_time") and c["to_get_time"] <= now + 60
        ]
    end_ts = parse_dt(str(info.get("end") or ""))
    ended = bool(info.get("is_end")) or bool(end_ts and end_ts <= now)
    if not ended:
        return []
    logger.warning(
        "refreshCode 没给 to_get_time（接口可能改版），改按「直播已结束」放行 %d 个码",
        len(codes),
    )
    return list(codes)


def live_title_short(game: str, title: str) -> str:
    """《崩坏：星穹铁道》4.7版本前瞻 → 4.7版本前瞻"""
    t = (title or "").strip()
    game_name = GAMES.get(game, "")
    if game_name and game_name in t:
        t = t.split(game_name, 1)[1]
    t = t.strip("《》 ·-—")
    return t or title or "前瞻直播"


def build_message(
    game: str,
    info: dict,
    codes: list[dict],
    expiry: str = "",
    release: str = "",
    span: dict | None = None,
) -> str:
    """凑成群里能直接看、能直接复制的一段话。"""
    game_name = GAMES.get(game) or info.get("game") or "米哈游"
    short = live_title_short(game, info.get("title", ""))
    lines = [f"【{game_name} · {short} 兑换码】"]

    marks = "①②③④⑤⑥⑦⑧⑨"
    for i, c in enumerate(codes):
        head = f"{marks[i] if i < len(marks) else str(i + 1) + '.'} {c['code']}"
        lines.append(head)
        if c.get("reward"):
            lines.append(f"   └ {c['reward']}")
    lines.append("")
    if expiry:
        lines.append(f"⏰ 有效期至 {expiry}，过期就作废了")
    else:
        lines.append("⏰ 一般次日 23:59 失效，早换早安心")
    if release:
        vm = _VERSION_RE.search(info.get("title", ""))
        ver = f"{vm.group(1)} 版本 " if vm else ""
        line = f"🗓 {ver}{release} 上线"
        # 这一版到哪天结束：来自游戏内公告「X.Y版本更新说明」的有效期。
        # 拿不到就不写，别硬按 42 天猜——星铁真出现过 33 天和 44 天的版本。
        if span and span.get("end_text"):
            line += f"，{span['end_text']} 结束（共 {span.get('days')} 天）"
        lines.append(line)
    lines.append("📮 游戏内 菜单 → 兑换码（或设置 → 账号 → 兑换码）")
    return "\n".join(lines)


# "2026年10月31日23:59:59失效" / "10月11日 23:59:59失效"（绝区零不写年份）
_EXPIRY_RE = re.compile(
    r"(?:(\d{4})\s*年\s*)?(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*(\d{1,2})\s*[:：]\s*(\d{2})"
    r"(?:\s*[:：]\s*(\d{2}))?\s*失效"
)

# "2026年10月30日19:30" / "10月9日 19:30"（预告帖里的直播时间）
_LIVE_TIME_RE = re.compile(
    r"(?:(\d{4})\s*年\s*)?(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*(\d{1,2})\s*[:：]\s*(\d{2})"
)

# "4.7版本...将于2026年11月11日上线" / "将于10月21日正式上线"（绝区零不写年份）
_RELEASE_RE = re.compile(
    r"(?:(\d{4})\s*年\s*)?(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*(?:版本)?(?:正式)?上线"
)

# "4.7版本" / "《原神》7.2版本" / "3.3版本「重返天空的旅程」"
_VERSION_RE = re.compile(r"(\d+\.\d+)")


def _build_ts(
    year: str | None, mo: str, dd: str, hh: str, mi: str, base_ts: float
) -> float:
    """拼时间戳。预告帖常常不写年份（"10月9日 19:30"），
    那就用发帖年份补；补出来比发帖还早很多，说明跨年了，加 1 年。"""
    y = int(year) if year else time.localtime(base_ts).tm_year
    try:
        ts = time.mktime((y, int(mo), int(dd), int(hh), int(mi), 0, 0, 0, -1))
    except (ValueError, OverflowError):
        return 0.0
    if not year and ts < base_ts - 86400 * 7:
        try:
            ts = time.mktime((y + 1, int(mo), int(dd), int(hh), int(mi), 0, 0, 0, -1))
        except (ValueError, OverflowError):
            return 0.0
    return ts


def _recent_official_posts(game: str, size: int = 30) -> list[dict]:
    """两家接口各取一份，合起来用。

    为什么要合：搜索接口按相关度排，帖子刚发出来那会儿常常还没收录
    （今晚 4.7 正片帖 21:11 发的，23:57 搜「前瞻特别节目」还搜不到）；
    而 userPost 是按时间倒序的原始列表，新的反而有，但官号一天发十条，
    往前翻三五天就到底了，三周前的预告又没有。两者互补。
    """
    uid = OFFICIAL_UID.get(game)
    if not uid:
        return []
    merged: dict[str, dict] = {}
    for loader in (
        lambda: _user_posts(uid, size),
        lambda: _official_posts(game, "前瞻特别节目", 20),
    ):
        try:
            for post in loader():
                pid = str(post.get("post_id") or "")
                if not pid:
                    continue
                subject = str(post.get("subject") or "")
                if "前瞻" not in subject:
                    continue
                # 保留正文更长的那一份（列表正文可能是被截断的摘要）
                old = merged.get(pid)
                if old is None or len(str(post.get("content") or "")) > len(
                    str(old.get("content") or "")
                ):
                    merged[pid] = post
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[miyo_codes] 拉 {game} 的帖子失败（换下一个源）：{exc}")
    posts = list(merged.values())
    posts.sort(key=lambda p: float(p.get("created_at") or 0), reverse=True)
    return posts


# 被限流后这段时间内别再碰米游社详情接口——越试恢复越慢，而且会把整轮轮询拖到几十秒
_throttle_until = 0.0


def _post_content_html(post_id: str, retries: int = 3) -> str:
    """帖子详情页的正文 HTML（不剥标签）——图片地址只在这里。

    搜索接口返回的 content 是纯文字摘要，连 <img> 都没有，
    所以要取「版本速览」的图必须再点进详情页。

    注意 retcode：米游社对详情接口限流，密集请求会返回 1034
    （此时 data 是空的、content 是 None）。**必须当错误处理**，
    否则会被当成"这帖没正文"，静默漏掉整期速览。而且不要在限流时反复重试：
    越试恢复越慢，所以命中 1034 就整体冷却几分钟，期间直接快速失败。
    """
    global _throttle_until
    if time.time() < _throttle_until:
        raise MiyoError("米游社详情接口仍在限流冷却中，先跳过")

    url = f"https://bbs-api.miyoushe.com/post/wapi/getPostFull?post_id={post_id}&read=1"
    last: Exception | None = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(
                url, headers={**HEADERS, "Referer": "https://www.miyoushe.com/"}
            )
            with urllib.request.urlopen(req, timeout=12) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            code = data.get("retcode")
            if code not in (0, None):
                raise MiyoError(f"retcode={code} {data.get('message') or ''}".strip())
            post = ((data.get("data") or {}).get("post") or {}).get("post") or {}
            return str(post.get("content") or "")
        except Exception as exc:
            last = exc
            if "retcode=1034" in str(exc):
                _throttle_until = time.time() + 300
                raise MiyoError(
                    f"读帖子 {post_id} 被限流（1034），5 分钟内不再请求详情接口"
                ) from exc
            time.sleep(2.0 * (i + 1))  # 其他错误（网络抖动）值得再试一次
    raise MiyoError(f"读帖子 {post_id} 正文失败：{last}")


def _post_full_content(post_id: str, retries: int = 2) -> str:
    """列表接口给的是截断正文（一百来字就没了），时间信息那句在后半段，
    所以要点进帖子详情才能读到。"""
    return strip_tags(_post_content_html(post_id, retries))


def _user_posts(uid: str, size: int = 20) -> list[dict]:
    url = f"https://bbs-api.miyoushe.com/post/wapi/userPost?size={size}&uid={uid}"
    req = urllib.request.Request(
        url, headers={**HEADERS, "Referer": "https://www.miyoushe.com/"}
    )
    with urllib.request.urlopen(req, timeout=12) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    return [it.get("post") or {} for it in (data.get("data") or {}).get("list") or []]


def _official_posts(
    game: str, keyword: str = "前瞻特别节目", size: int = 20
) -> list[dict]:
    """搜「前瞻特别节目」，只留这家官号的帖子，按发帖时间倒序。

    为什么不用 userPost：绝区零官号一天能发十条，20 条只覆盖两三天，
    而预告帖是直播前 4 天发的，早就被挤出去了。搜索接口能翻到几个月前。
    """
    uid = OFFICIAL_UID.get(game)
    gids = GAME_GIDS.get(game)
    if not uid or not gids:
        return []
    url = (
        "https://bbs-api.miyoushe.com/post/wapi/searchPosts"
        f"?keyword={urllib.parse.quote(keyword)}&size={size}&gids={gids}"
    )
    req = urllib.request.Request(
        url, headers={**HEADERS, "Referer": "https://www.miyoushe.com/"}
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    posts = [it.get("post") or {} for it in (data.get("data") or {}).get("posts") or []]
    posts = [p for p in posts if str(p.get("uid") or "") == uid]
    posts.sort(key=lambda p: float(p.get("created_at") or 0), reverse=True)
    return posts


def fetch_expiry(game: str) -> str:
    """从官方前瞻帖的正文里抠"兑换码将于 XXX 失效"。抠不到就返回空串。

    三家给的时长天差地别，所以这个字段值得单独去读：
        星铁    次日 23:59:59（约 27 小时）
        原神    第 3 天 12:00（约 64 小时）
        绝区零  第 2 天 23:59:59（约 52 小时）
    """
    if not OFFICIAL_UID.get(game):
        return ""
    try:
        # 演出结束后官方会发「前瞻特别节目」正片帖，失效时间写在里面。
        # 回顾长图里也有，所以不限定"正片"，按时间倒序挨个看，谁先抠到算谁。
        posts = _recent_official_posts(game)
    except Exception:  # noqa: BLE001  失效时间只是锦上添花，拿不到不影响发码
        return ""

    for post in posts:
        subject = str(post.get("subject") or "")
        if "预告" in subject:
            continue  # 预告帖里没有兑换码信息
        created = float(post.get("created_at") or time.time())
        if time.time() - created > 30 * 86400:
            break  # 只认这一个月内的场次
        body = str(post.get("content") or "")
        if "失效" not in body:
            try:
                body = _post_full_content(str(post.get("post_id") or ""))
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[miyo] 预告帖正文读取失败，跳过该帖：{exc}")
                continue
        else:
            body = strip_tags(body)
        m = _EXPIRY_RE.search(body)
        if m:
            ts = _build_ts(
                m.group(1),
                m.group(2),
                m.group(3),
                m.group(4),
                m.group(5),
                created,
            )
            if ts:
                return time.strftime("%m-%d %H:%M", time.localtime(ts))
    return ""


def fetch_show_meta(game: str) -> dict:
    """一场前瞻结束后，官方正片帖里能读到两样东西——一次搜索全拿回来。

    1. 兑换码什么时候失效
    2. **这个新版本哪天上线**（也就是「当前版本的结束日」）

    第 2 条比什么都有用：版本周期并不固定（星铁 4.5→4.6 只有 33 天，4.6→4.7 是 44 天），
    所以"每 6 周一次"只能当参考；真正靠谱的时间锚点，是每次前瞻里公布的这条上线日期。
    """
    out = {"expiry": "", "release_text": "", "release_ts": 0.0, "version": ""}
    if not OFFICIAL_UID.get(game):
        return out
    try:
        posts = _recent_official_posts(game)
    except Exception:  # noqa: BLE001  是锦上添花，拿不到不影响发码
        return out

    for post in posts:
        subject = str(post.get("subject") or "")
        if "预告" in subject:
            continue  # 预告帖里没有兑换码和上线日期
        created = float(post.get("created_at") or time.time())
        if time.time() - created > 30 * 86400:
            break  # 只认这一个月内的场次
        body = str(post.get("content") or "")
        if "失效" not in body or "上线" not in body:
            try:
                body = _post_full_content(str(post.get("post_id") or ""))
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[miyo] 版本速览正文读取失败，跳过该帖：{exc}")
                continue
        else:
            body = strip_tags(body)

        vm = _VERSION_RE.search(subject)
        if vm and not out["version"]:
            out["version"] = vm.group(1)

        if not out["expiry"]:
            m = _EXPIRY_RE.search(body)
            if m:
                ts = _build_ts(
                    m.group(1), m.group(2), m.group(3), m.group(4), m.group(5), created
                )
                if ts:
                    out["expiry"] = time.strftime("%m-%d %H:%M", time.localtime(ts))

        if not out["release_ts"]:
            r = _RELEASE_RE.search(body)
            if r:
                ts = _build_ts(r.group(1), r.group(2), r.group(3), "12", "00", created)
                if ts:
                    out["release_ts"] = ts
                    out["release_text"] = time.strftime("%m-%d", time.localtime(ts))

        if out["expiry"] and out["release_ts"]:
            break
    return out


def fetch_notice(game: str) -> dict | None:
    """读官方最新一条「前瞻特别节目预告」，解析出直播时间。

    为什么只信预告、不按星期几推：直播确实大多在周五晚，但改过很多次——
    星铁 4.6 是周日、原神 7.1 是周六、原神春节档还放在中午 12:00。
    星期几只能用来猜"大概哪天"，真正的开播时间必须以预告为准。

    返回 {live_at, live_at_text, version, subject, post_id}，找不到返回 None。
    """
    if not OFFICIAL_UID.get(game):
        return None
    try:
        posts = _recent_official_posts(game)
    except Exception:  # noqa: BLE001
        return None

    for post in posts:
        subject = str(post.get("subject") or "")
        if "前瞻" not in subject or "预告" not in subject:
            continue
        created = float(post.get("created_at") or time.time())
        # 只看最近 3 个月发的，太老的预告没有意义
        if time.time() - created > 90 * 86400:
            break
        body = strip_tags(str(post.get("content") or ""))
        if not _LIVE_TIME_RE.search(body):
            # 列表里的正文是一百来字的摘要，开播时间那句通常在后面，进详情页读全文
            try:
                body = _post_full_content(str(post.get("post_id") or ""))
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[miyo] 前瞻帖正文读取失败，跳过该帖：{exc}")
                continue
        m = _LIVE_TIME_RE.search(body)
        if not m:
            continue
        ts = _build_ts(
            m.group(1), m.group(2), m.group(3), m.group(4), m.group(5), created
        )
        if not ts:
            continue
        vm = _VERSION_RE.search(subject)
        return {
            "live_at": ts,
            "live_at_text": time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)),
            "weekday": "周" + "一二三四五六日"[time.localtime(ts).tm_wday],
            "version": vm.group(1) if vm else "",
            "subject": subject,
            "post_id": str(post.get("post_id") or ""),
            "notice_ts": created,
        }
    return None


# -------------------------------------------- 版区工具列表（act_id 自动发现）
# 米游社 PC 网页版每个版区右侧有一排工具按钮，其中「直播兑换码」那一项只在
# 前瞻直播前后出现，它的跳转链接里直接带 act_id：
#
#   .../bbs/event/live/index.html?act_id=ea202610261626345889&game_biz=hkrpg
#
# 实测（星铁 4.7）：按钮 10-30 18:00 上线、10-31 23:00 下线，共 29 小时，
# 比直播开始早 2 小时、比兑换码失效早 1 小时。也就是说它一出现，act_id 就到手了，
# 不用等人贴链接，也不用登录 —— 这是目前唯一能自动拿到 act_id 的公开来源。
#
# 这个接口对「谁在问」很敏感，坑在这里：
# 同一个 game_id、同样的 types，请求头里的 x-rpc-client_type 不同，
# 返回的就是两份完全不同的工具表。实测（2026-10-31）：
#
#   端          client_type   原神    星铁              绝区零
#   网页            4         8 项    10 项（有兑换码）   7 项
#   安卓 App        2        10 项    10 项（有兑换码）  10 项
#   iOS App         1        10 项    10 项（有兑换码）  10 项
#
# 不带 client_type（或者带 0/3/5/6）时，原神直接返回空列表、绝区零退回 7 项。
# 这就是"网页端明明看得见工具区，接口却给空"的原因 —— 不是版区没配，是没表明身份。
#
# 所以两家都要问：网页端那份和 App 端那份内容不一样（原神网页端有「阵容广场」
# 「卡牌广场」，App 端有「旅行工具」「绮星盛会」「归忆星旅」），
# 直播兑换码可能只挂在其中一份上。谁先给出来就用谁。
#
# 绝区零：窗口外扫到的都是普通工具（签到福利/企业微信/养成指南/战绩/米游铺/
# 情报站/绳网月报，App 端另有攻略合集、支付中心、工具箱），没有这一项。
# 但**这不构成"没有"的证据** —— 3.3 前瞻是 10-09、码 10-11 失效，扫的时候
# 早过了窗口；而星铁同样在窗口外也扫不到。所以绝区零现在只能算"未验证"，
# 等 3.4 前瞻（约 12 月）那 29 小时里再看一眼才算数。fetch_nav_survey() 会把
# 每次扫到的工具名带回来，就是为了到时候能拿事实判断，而不是靠推断。

NAV_API = "https://bbs-api.miyoushe.com/misc/wapi/getUniversalNavigators"
NAV_PC_TYPE = 4  # 4 = 工具类导航（跟 client_type 不是一回事，别混）
_NAV_ACT_RE = re.compile(r"act_id=([A-Za-z0-9]{6,40})")

# 按顺序问，拿到 act_id 就停。网页端排前面：版区页面上看得见的那份就是它，
# 出问题时对着页面排查最直观。
_NAV_CLIENTS = (
    ("web", "4"),
    ("android", "2"),
)


def _nav_groups(gid: int, client_type: str) -> list[dict]:
    """以指定客户端身份要一次版区工具列表。"""
    body = json.dumps({"game_id": gid, "types": [NAV_PC_TYPE]}).encode()
    req = urllib.request.Request(
        NAV_API,
        data=body,
        headers={
            **HEADERS,
            "Content-Type": "application/json",
            "Referer": "https://www.miyoushe.com/",
            "Origin": "https://www.miyoushe.com",
            "x-rpc-client_type": client_type,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=12) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    if data.get("retcode") != 0:
        raise MiyoError(str(data.get("message") or data.get("retcode")))
    return (data.get("data") or {}).get("list") or []


def _scan_for_live(groups: list[dict]) -> dict | None:
    """在一份工具表里找「直播兑换码」，返回它带的 act_id。"""
    for group in groups:
        for nav in group.get("navigators") or []:
            name = str(nav.get("name") or "")
            path = str(nav.get("web_path") or nav.get("app_path") or "")
            # 认两种特征：名字里带「兑换码」，或者跳转目标就是直播页
            if "兑换码" not in name and "/bbs/event/live/" not in path:
                continue
            hit = _NAV_ACT_RE.search(path)
            if not hit:
                continue
            return {
                "act_id": hit.group(1),
                "name": name,
                "online_ts": int(nav.get("online_ts") or 0),
                "offline_ts": int(nav.get("offline_ts") or 0),
                "web_path": path,
            }
    return None


def fetch_nav_survey(game: str) -> dict:
    """把版区工具列表整个问一遍，返回「有没有兑换码按钮」+「都扫到了哪些工具」。

    为什么要连工具名一起带回来：这个按钮只在直播窗口里存在（星铁 4.7 实测是
    29 小时），窗口外查不到是正常的。只看拿没拿到 act_id，就分不清
    「窗口还没开」和「这家压根没配这一项」——结论只能靠猜。
    带上工具名之后，下一次窗口期一过就能用事实说话（有 / 没有）。

    返回：
        {"act_id": str, "name": str, "offline_ts": int, "web_path": str,
         "clients": {"网页": [工具名...], "安卓": [...]},
         "errors":  {"网页": "错误原因"}}
    """
    out: dict = {
        "act_id": "",
        "name": "",
        "offline_ts": 0,
        "web_path": "",
        "clients": {},
        "errors": {},
    }
    gid = GAME_GIDS.get(game)
    if not gid:
        return out
    for label, client_type in _NAV_CLIENTS:
        try:
            groups = _nav_groups(gid, client_type)
        except Exception as exc:  # noqa: BLE001
            # 一份问不到就试下一份，别让单个客户端的问题拖垮整轮
            out["errors"][label] = str(exc)
            logger.info(f"[miyo_codes] {game} 的 {label} 工具列表请求失败：{exc}")
            continue
        names = [
            str(nav.get("name") or "")
            for group in groups
            for nav in group.get("navigators") or []
        ]
        out["clients"][label] = names
        hit = _scan_for_live(groups)
        if hit and not out["act_id"]:
            out.update(hit)
    # 只有每一份都请求失败才算真出错。能问到、但列表里没有那一项，
    # 是正常状态（一年里大部分时间都没有），不该让上层当成故障退避。
    if not out["clients"] and out["errors"]:
        raise MiyoError(next(iter(out["errors"].values())))
    return out


def fetch_nav_act_id(game: str) -> dict | None:
    """从版区工具列表里找「直播兑换码」，返回它带的 act_id。

    按钮不在线时返回 None —— 这是常态（一年里只有几十个小时在线），不是失败。
    接口很轻：POST 一个两字段的 JSON，返回几 KB，不需要登录也不需要签名。
    """
    survey = fetch_nav_survey(game)
    if not survey.get("act_id"):
        return None
    return {
        "act_id": survey["act_id"],
        "name": survey.get("name", ""),
        "online_ts": int(survey.get("online_ts") or 0),
        "offline_ts": int(survey.get("offline_ts") or 0),
        "web_path": survey.get("web_path", ""),
    }


# ---------------------------------------------------------------- 版本周期（OCR）
# 一个版本到底多少天，官方唯一会「提前」说的地方就是「版本速览」长图：
#
#     版本更新  09/28          版本结束  11/11
#
# 这行字是画在图里的，帖子里一个字都没有，所以只能 OCR。
# 为什么不按 42 天推算：星铁 4.5 只有 33 天（08/26 → 09/28）、4.6 是 44 天
# （09/28 → 11/11），拿 6 周硬推会直接把下一次前瞻算错。

# 哪款游戏有这个「速览」长图。绝区零的《情报总览》是 15000px 的长条海报，
# 日期不在同一块版面上，原神则根本没有这类长图，所以两家先不猜——
# 它们历来是 42 天整，用常态值就够准。
_SUMMARY_KW = {"hkrpg": "版本速览"}


def supports_version_span(game: str) -> bool:
    """这家的版本周期能不能自动读。

    游戏内公告三家都有（首选路径）；速览长图只有星铁有（OCR 兜底路径）。
    """
    return game in ANN_API or game in _SUMMARY_KW


_MMDD_RE = re.compile(r"^(\d{1,2})\s*[/／.]\s*(\d{1,2})$")
_IMG_SRC_RE = re.compile(r'<img[^>]+src="(https?://[^"]+)"', re.IGNORECASE)

_ocr_engine = None


def _ocr():
    """OCR 引擎很重（模型加载一两秒），全局只建一次；没装就返回 None。

    没装时不报错、只是「版本周期」这一项功能缺失，取码发码完全不受影响。
    """
    global _ocr_engine
    if _ocr_engine is None:
        try:
            from rapidocr_onnxruntime import RapidOCR

            _ocr_engine = RapidOCR()
        except Exception as exc:  # noqa: BLE001
            logger.info(f"[miyo_codes] 没装 OCR，版本周期功能跳过：{exc}")
            _ocr_engine = False
    return _ocr_engine or None


def _img_size(raw: bytes) -> tuple[int, int]:
    import io

    from PIL import Image

    with Image.open(io.BytesIO(raw)) as im:
        return im.size


def _ocr_rows(raw: bytes, top_ratio: float = 0.35) -> list[tuple]:
    """只 OCR 图片顶部那一带（日期栏就在标题下面），返回 [(cx, cy, 文本)]。

    整张 900×2200 的图全量 OCR 要七八秒，只切顶部 35% 两秒就够了。
    """
    import io

    import numpy as np
    from PIL import Image

    engine = _ocr()
    if engine is None:
        return []
    try:
        with Image.open(io.BytesIO(raw)) as im:
            w, h = im.size
            crop = im.crop((0, 0, w, max(240, int(h * top_ratio)))).convert("RGB")
        result, _ = engine(np.array(crop))
    except Exception as exc:  # noqa: BLE001
        logger.info(f"[miyo_codes] OCR 失败（跳过这张图）：{exc}")
        return []

    rows = []
    for item in result or []:
        try:
            box, text = item[0], str(item[1])
        except (IndexError, TypeError):
            continue
        xs = [float(p[0]) for p in box]
        ys = [float(p[1]) for p in box]
        rows.append(((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2, text.strip()))
    return rows


def _span_from_rows(
    rows: list[tuple],
) -> tuple[tuple[int, int], tuple[int, int]] | None:
    """在 OCR 结果里定位「版本更新」和「版本结束」，各取正下方最近的那串 月/日。

    版面是这样的（日期在标签正下方，中间夹着版本更新周期的分期日期）：
        版本更新        09/28   09/12   版本结束
    所以不能简单取第一个和最后一个，要按 x 坐标对齐。
    """
    labels = {}
    for cx, cy, text in rows:
        for key in ("版本更新", "版本结束"):
            if key not in labels and key in text:
                labels[key] = (cx, cy)

    dates = []
    for cx, cy, text in rows:
        m = _MMDD_RE.match(text.replace(" ", ""))
        if m:
            dates.append((cx, cy, int(m.group(1)), int(m.group(2))))
    if len(dates) < 2:
        return None

    def pick(label_key: str):
        pos = labels.get(label_key)
        if not pos:
            return None
        lx, ly = pos
        band = [d for d in dates if abs(d[1] - ly) <= 160] or dates
        return min(band, key=lambda d: abs(d[0] - lx))

    start, end = pick("版本更新"), pick("版本结束")
    if not start or not end or start is end:
        return None
    return (start[2], start[3]), (end[2], end[3])


def _mmdd_ts(mo: int, dd: int, base_ts: float) -> float:
    """把 09/28 这种没年份的写法补成时间戳。速览图都是「发帖之后」的日期，
    补出来比发帖还早 40 天以上，说明跨年了。"""
    year = time.localtime(base_ts or time.time()).tm_year
    try:
        ts = time.mktime((year, mo, dd, 0, 0, 0, 0, 0, -1))
        if ts < (base_ts or 0) - 40 * 86400:
            ts = time.mktime((year + 1, mo, dd, 0, 0, 0, 0, 0, -1))
    except (ValueError, OverflowError):
        return 0.0
    return ts


def _fetch_bytes(url: str, timeout: float = 20.0) -> bytes:
    req = urllib.request.Request(
        url, headers={**HEADERS, "Referer": "https://www.miyoushe.com/"}
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_version_span(
    game: str,
    cached: dict | None = None,
    start_hint: float = 0.0,
    version_hint: str = "",
) -> dict | None:
    """读当前版本的起止日期（版本更新日 ~ 版本结束日）。

    两条路，先快后慢：

    1. **游戏内公告**（首选）：`X.Y版本更新说明` 的结束时间就是这一版的结束日。
       结构化 JSON、免登录、三家通用、不用 OCR，几百毫秒就出结果。
    2. **版本速览长图 OCR**（兜底）：目前只有星铁有这张图，要下载再识别，两三秒。

    必须和「本版版本号」对上才算数：新版本上线前，公告里还是上一版的
    「更新说明」，它的结束日是新版本的上线日，不是新版本的结束日。
    所以 ann.version 必须等于 version_hint（本版），否则说明公告还没切过来。

    返回 {version, start_text, end_text, start_ts, end_ts, days, post_id, subject, src}。
    读不到返回 None；cached 传上次结果，同一篇速览帖直接复用，省一次 OCR。
    """
    now = time.time()

    # ---- ① 先走游戏内公告：连起点都能互相推，一行 OCR 都不用做 ----
    spans: list[dict] = []
    try:
        spans = fetch_ann_spans(game, now)
    except Exception as exc:  # noqa: BLE001
        logger.info(f"[miyo_codes] {game} 公告这条路没走通：{exc}")

    if spans:
        latest = spans[-1]
        end_ts = float(latest["end_ts"])
        cur_ver = str(version_hint or "")
        start_ts = 0.0
        # 起点三选一：
        #   ① 前瞻帖公布的本版上线日（只有版本号对得上才可信）
        #   ② 上一版的结束日——它正好是这一版的开始日，公告自己就能给出
        #   ③ 上次算出来的
        if start_hint and (not cur_ver or cur_ver == latest["version"]):
            start_ts = float(start_hint)
        elif len(spans) >= 2:
            start_ts = float(spans[-2]["end_ts"])
        elif cached and cached.get("start_ts"):
            start_ts = float(cached["start_ts"])

        if start_ts and 10 * 86400 <= end_ts - start_ts <= 80 * 86400:
            days = round((end_ts - start_ts) / 86400)
            return {
                "version": latest["version"],
                "start_ts": start_ts,
                "start_text": time.strftime("%m-%d", time.localtime(start_ts)),
                "end_ts": end_ts,
                "end_text": latest["end_text"],
                "days": days,
                "post_id": f"ann:{latest.get('ann_id')}",
                "subject": latest.get("title", ""),
                "src": "ann",
            }
        # 有公告却拼不出周期，说明新版还没上线（公告还停在上一版）。
        # 这时候别再转去 OCR——那是另一个接口，还更容易被限流，而且同样读不到新版。
        return dict(cached) if cached else None

    # ---- ② 公告完全读不到，退回 OCR 速览长图（只有星铁有） ----
    keyword = _SUMMARY_KW.get(game)
    if not keyword:
        return None
    try:
        posts = _official_posts(game, keyword, 20)
    except Exception as exc:  # noqa: BLE001
        logger.info(f"[miyo_codes] 搜 {game} 的版本速览失败：{exc}")
        return None

    now = time.time()
    for post in posts[:4]:
        pid = str(post.get("post_id") or "")
        if not pid:
            continue
        subject = str(post.get("subject") or "")
        if "前瞻" in subject:
            continue  # 「前瞻节目速览」只有节目单，没有版本日期表
        created = float(post.get("created_at") or 0) or now
        if now - created > 120 * 86400:
            continue
        if cached and str(cached.get("post_id") or "") == pid and cached.get("end_ts"):
            return dict(cached)

        vm = _VERSION_RE.search(subject)
        images = _IMG_SRC_RE.findall(str(post.get("content") or ""))
        if not images:
            try:
                images = _IMG_SRC_RE.findall(_post_content_html(pid))
            except MiyoError as exc:
                # 多半是被限流（retcode 1034）。这时候别再试下一帖，越试越黑；
                # 返回旧结果，等半小时后再来。
                logger.info(f"[miyo_codes] {game} 的速览帖这会儿读不动：{exc}")
                return dict(cached) if cached else None
        if not images:
            continue
        for url in images[:4]:
            try:
                raw = _fetch_bytes(url)
                w, h = _img_size(raw)
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"[miyo] 版本速览配图取不到，跳过：{exc}")
                continue
            # 头图 690×320 那种没有日期；15000px 的长海报 OCR 又慢又不准，都跳过
            if w < 500 or h < 600 or h > 4500:
                continue
            pair = _span_from_rows(_ocr_rows(raw))
            if not pair:
                continue
            ocr_start = _mmdd_ts(pair[0][0], pair[0][1], created)
            ocr_end = _mmdd_ts(pair[1][0], pair[1][1], created)
            if not ocr_start or not ocr_end or ocr_end <= ocr_start:
                continue
            days = round((ocr_end - ocr_start) / 86400)
            if not 10 <= days <= 80:
                continue
            return {
                "version": vm.group(1) if vm else "",
                "start_ts": ocr_start,
                "start_text": time.strftime("%m-%d", time.localtime(ocr_start)),
                "end_ts": ocr_end,
                "end_text": time.strftime("%m-%d", time.localtime(ocr_end)),
                "days": days,
                "post_id": pid,
                "subject": subject,
                "src": "ocr",
            }
    return None


# ---------------------------------------------------------------- 游戏内公告
# 比「版本速览」长图更好的一条路：游戏内公告里，「X.Y版本更新说明 / 更新公告」
# 的结束时间就是这一版的结束时刻。三家都成立，实测（2026-10-31 抓取）：
#
#     星铁  4.6版本更新说明    ~ 2026-11-11 07:00  → 4.7 于 11-11 上线  ✓
#     原神  7.1版本更新说明    ~ 2026-11-04 06:00  → 7.2 于 11-04 上线  ✓
#     绝区零 3.3版本更新公告    ~ 2026-12-02 06:00  → 3.3 共 42 天       ✓
#
# 好处：免登录（uid 随便填都行）、结构化 JSON、不用 OCR、三家通用。
# 而且它比速览长图更早：星铁「4.7版本商店上新」在前瞻当晚 20:55 就发了，
# 结束时间直接写着 12-23。

ANN_API = {
    "hkrpg": (
        "https://hkrpg-api.mihoyo.com/common/hkrpg_cn/announcement/api/getAnnList"
        "?game=hkrpg&game_biz=hkrpg_cn&lang=zh-cn&bundle_id=hkrpg_cn"
        "&platform=pc&region=prod_gf_cn&level=70&uid=100000000"
    ),
    "ys": (
        "https://hk4e-api.mihoyo.com/common/hk4e_cn/announcement/api/getAnnList"
        "?game=hk4e&game_biz=hk4e_cn&lang=zh-cn&bundle_id=hk4e_cn"
        "&platform=pc&region=cn_gf01&level=60&uid=100000000"
    ),
    "nap": (
        "https://announcement-api.mihoyo.com/common/nap_cn/announcement/api/getAnnList"
        "?game=nap&game_biz=nap_cn&lang=zh-cn&bundle_id=nap_cn"
        "&platform=pc&region=prod_gf_cn&level=60&uid=100000000"
    ),
}

# 标题里带这些词的公告，结束时间跟版本无关（问卷、长期声明、玩家工具）
_ANN_NOISE = (
    "调研",
    "问卷",
    "防沉迷",
    "声明",
    "小程序",
    "社区",
    "FAQ",
    "工具一览",
    "聚合页",
    "常见问题",
    "签到",
    "周边",
)


def _ann_ts(text: str) -> float:
    """公告时间是 "2026-12-23 06:00:00" 这种本地时间。"""
    text = str(text or "").strip()
    if len(text) < 19:
        return 0.0
    try:
        return time.mktime(time.strptime(text[:19], "%Y-%m-%d %H:%M:%S"))
    except ValueError:
        return 0.0


def _ver_key(ver: str) -> tuple[int, ...]:
    """4.10 要比 4.9 大，所以按数字段比，不能按字符串比。"""
    parts: list[int] = []
    for seg in str(ver).replace("．", ".").split("."):
        try:
            parts.append(int(seg))
        except ValueError:
            parts.append(0)
    return tuple(parts) or (0,)


def fetch_ann_spans(game: str, now: float | None = None) -> list[dict]:
    """把游戏内公告按版本号归并，返回升序的 [{version, end_ts, end_text, title, ann_id}]。

    撑起整条路的只有一条规律：**「X.Y版本更新说明」的结束时间 = X.Y 的结束时刻**。
    而上一版的结束时刻又正好是这一版的开始时刻——所以只要公告里同时有 N 和 N+1，
    连「这一版哪天开始」都不用问别人，自己就能拼出周期。

    同一版本会有好几条公告（更新说明、游戏优化、新增关卡…），全在版本结束那天到期，
    取最晚的那个即可。
    """
    url = ANN_API.get(game)
    if not url:
        return []
    now = now or time.time()
    try:
        req = urllib.request.Request(
            url, headers={**HEADERS, "Referer": "https://www.miyoushe.com/"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001
        logger.info(f"[miyo_codes] 读 {game} 的游戏内公告失败：{exc}")
        return []
    if str(data.get("retcode")) not in ("0", "None"):
        logger.info(f"[miyo_codes] {game} 公告接口返回 {data.get('retcode')}")
        return []

    # 只留「还没过期、又不会远得离谱」的；长期公告的结束年在 2035/2036
    horizon = now + 180 * 86400
    best: dict[str, tuple[float, str, str]] = {}
    for group in (data.get("data") or {}).get("list") or []:
        for item in group.get("list") or []:
            title = strip_tags(str(item.get("title") or ""))
            if any(k in title for k in _ANN_NOISE):
                continue
            vm = _VERSION_RE.search(title)
            if not vm:
                continue
            end_ts = _ann_ts(item.get("end_time"))
            if not end_ts or end_ts <= now or end_ts > horizon:
                continue
            ver = vm.group(1)
            old = best.get(ver)
            if not old or end_ts > old[0]:
                best[ver] = (end_ts, title, str(item.get("ann_id") or ""))

    spans = [
        {
            "version": ver,
            "end_ts": ts,
            "end_text": time.strftime("%m-%d", time.localtime(ts)),
            "title": title,
            "ann_id": ann_id,
        }
        for ver, (ts, title, ann_id) in best.items()
    ]
    spans.sort(key=lambda r: _ver_key(r["version"]))
    return spans
