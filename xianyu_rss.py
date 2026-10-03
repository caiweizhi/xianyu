#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""闲鱼上新监控 -> RSS

零第三方依赖：只用标准库（urllib / http.cookiejar / hashlib / json）。
链路：storage-state.json 里的 cookie -> mtop 搜索接口(md5 签名) -> 按 itemId 去重 -> 生成 RSS 2.0

用法（不带参数 = all）：
  python xianyu_rss.py all      常驻：后台定时抓取 + 前台提供 RSS（推荐，一条命令搞定）
  python xianyu_rss.py once     抓一轮，写 feeds/*.xml 后退出（可挂 cron / 任务计划）
  python xianyu_rss.py loop     只循环抓取，不提供 HTTP
  python xianyu_rss.py serve    只提供 HTTP，不抓取
  python xianyu_rss.py check    只验证登录态与接口是否可用
"""

from __future__ import annotations

import email.utils
import hashlib
import http.cookiejar as cookiejar
import json
import os
import pathlib
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from xml.sax.saxutils import escape

# ── 接口常量 ────────────────────────────────────────────────────────────────
APP_KEY = "34839810"
API = "mtop.taobao.idlemtopsearch.pc.search"
ENDPOINT = f"https://h5api.m.goofish.com/h5/{API}/1.0/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
ITEM_URL = "https://www.goofish.com/item?id="

def _force_utf8_console() -> None:
    """容器里 LANG 常常是 C/POSIX，stdout 会用 ascii 编解码，打印中文直接崩。
    这里强制切成 UTF-8，保证在 Docker / 青龙 / 任何环境下日志都不炸。"""
    for stream in ("stdout", "stderr"):
        s = getattr(sys, stream, None)
        try:
            if s and hasattr(s, "reconfigure"):
                s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_force_utf8_console()

_script_dir = pathlib.Path(__file__).resolve().parent
# 青龙等场景下想把配置/状态放到别处持久化时，用 XIANYU_RSS_DIR 覆盖
_env_dir = os.environ.get("XIANYU_RSS_DIR", "").strip()
BASE = pathlib.Path(_env_dir).resolve() if _env_dir else _script_dir
DEFAULT_CONFIG = BASE / "config.json"

DEFAULT_CONFIG_OBJ = {
    "cookie_state": "storage-state.json",
    "interval_seconds": 300,
    "feed_size": 100,
    "rows_per_page": 30,
    "pages": 1,
    "request_delay": [1.5, 4.0],
    "output_dir": "feeds",
    "state_file": "state.json",
    "serve_host": "0.0.0.0",
    "serve_port": 8899,
    "notify": False,          # 青龙面板推送；本地无 notify 模块会静默跳过
    "tasks": [{"keyword": "macmini"}],
}


# ── 工具 ────────────────────────────────────────────────────────────────────
def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _pid_alive(pid: int) -> bool:
    """跨平台判断进程是否还活着（用于识别被强杀留下的孤儿锁）。"""
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        if os.name == "nt":
            import ctypes

            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if not h:  # pid 不存在
                return False
            ctypes.windll.kernel32.CloseHandle(h)
            return True

        # Linux：优先读 /proc/<pid>/cmdline。容器里 PID 编号很小且循环复用，
        # os.kill(pid, 0) 可能对"恰好用了同一个号"的其他进程返回 True，
        # 那样会误判成"上一轮还在跑"而白白跳过。有 cmdline 内容才算真的活着。
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as fp:
                return bool(fp.read().rstrip(b"\x00"))
        except OSError:
            pass  # 没有 /proc（macOS 等），退回传统方式

        os.kill(pid, 0)
        return True
    except Exception:
        return False


class FileLock:
    """防止 cron 上一轮没跑完下一轮就启动（会加倍请求量，容易被风控）。
    拿不到锁就跳过本轮。锁会在两种情况下被接管：

    1. 属主进程已经不存在（上次是被强杀 / 崩了），立即接管，不用等满 15 分钟
    2. 锁文件超过 stale 秒没动过（兜底，处理读不到 PID 的情况）
    """

    def __init__(self, path: pathlib.Path, stale: int = 900):
        self.path = path
        self.stale = stale
        self.held = False

    def acquire(self) -> bool:
        try:
            fd = os.open(str(self.path),
                         os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            self.held = True
            return True
        except FileExistsError:
            try:
                owner = self.path.read_text(encoding="utf-8").strip()
                pid = int(owner) if owner.isdigit() else 0
                if pid and not _pid_alive(pid):  # 属主已死 = 陈旧锁
                    self.path.unlink()
                    return self.acquire()
            except Exception:
                pass
            try:
                if time.time() - self.path.stat().st_mtime > self.stale:
                    self.path.unlink()
                    return self.acquire()
            except Exception:
                pass
            return False
        except Exception:
            return True  # 不支持的平台不阻塞执行

    def release(self) -> None:
        if self.held:
            try:
                self.path.unlink()
            except Exception:
                pass


"""青龙面板 notify.py 可能所在的目录（不同版本位置不同），逐个试。

实测新版青龙的通知模块真实路径是 ``/ql/shell/preload/notify.py``
（见 whyour/qinglong 里脚本 import notify 时的报错栈），旧版可能是 ``/ql/shell``。
"""
_QL_NOTIFY_PATHS = (
    "/ql/shell/preload",
    "/ql/shell",
    os.path.expanduser("~/ql/shell/preload"),
)


def _warn_if_proxied() -> None:
    """青龙里常有人配全局代理（为拉 GitHub 脚本），而 urllib 默认尊重 *_proxy
    环境变量 —— 那样闲鱼请求也会被塞进代理，轻则失败，重则出口 IP 跳到境外被风控。"""
    env = os.environ
    proxy = (env.get("https_proxy") or env.get("HTTPS_PROXY")
             or env.get("http_proxy") or env.get("HTTP_PROXY")
             or env.get("all_proxy") or env.get("ALL_PROXY") or "")
    if not proxy:
        return
    no_proxy = env.get("no_proxy") or env.get("NO_PROXY") or ""
    if "goofish" in no_proxy:
        return
    log(f"[注意] 检测到代理 {proxy}，闲鱼请求会走代理")
    log("      抓取若失败，请加环境变量 NO_PROXY=goofish.com,.goofish.com，或临时关掉代理")


def notify(title: str, body: str, enabled: bool = False) -> None:
    """青龙面板自带通知通道（已配好推送方式时生效）；
    本地运行没有 notify 模块，静默跳过，不影响主流程。"""
    if not enabled:
        return
    try:
        for p in _QL_NOTIFY_PATHS:
            if p not in sys.path and os.path.isdir(p):
                sys.path.append(p)
        from notify import send  # type: ignore  # noqa: PLC0415
        send(title, body)
        send(title, body)
    except Exception:
        pass


def load_config(path: pathlib.Path) -> dict:
    cfg = dict(DEFAULT_CONFIG_OBJ)
    if path.exists():
        cfg.update(json.loads(path.read_text(encoding="utf-8")))
    else:
        path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"已生成默认配置: {path}")
    return cfg


_UNSAFE_FS = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def task_slug(task: dict) -> str:
    """每个任务的 feed 名（用于文件名 + URL），取值范围：

    1. 任务里显式配的 ``name``（推荐给中文关键词配一个英文别名）
    2. keyword 本身就是纯 ASCII（如 macmini）时直接用
    3. 否则退化成 x<sha1 前 6 位>，保证稳定且不会重名

    注意：**绝不用百分号编码做文件名**——那会让 URL 二次编码成 %25xx。
    """
    raw = str(task.get("name") or "").strip()
    if not raw:
        raw = str(task.get("keyword") or "").strip()
    s = re.sub(r"[^A-Za-z0-9_-]+", "-", raw.lower()).strip("-")
    if s:
        return s
    return "x" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:6]


def _safe_filename(slug: str) -> str:
    """再兜一层：文件名里不该出现路径分隔符等危险字符。"""
    return _UNSAFE_FS.sub("-", slug) or "feed"


def feed_url(slug: str, base: str = "") -> str:
    """拼订阅地址。中文/特殊字符只在这里做**一次**百分号编码。"""
    name = urllib.parse.quote(slug + ".xml", safe="")
    return f"{base}/{name}" if base else name


def price_text(ex: dict) -> str:
    segs = ex.get("price") or []
    if isinstance(segs, list):
        return "".join(s.get("text", "") for s in segs if isinstance(s, dict)).strip()
    return ""


def price_num(ex: dict) -> float | None:
    raw = (ex.get("detailParams") or {}).get("soldPrice") or ""
    try:
        return float(str(raw).replace(",", ""))
    except (TypeError, ValueError):
        return None


def http_date(ts_ms: int | None) -> str:
    try:
        return email.utils.formatdate(float(ts_ms) / 1000.0, usegmt=True)
    except (TypeError, ValueError):
        return email.utils.formatdate(usegmt=True)


def _ret_hint(ret: str) -> str:
    """把 mtop 的错误码翻译成人话 + 该做什么。"""
    r = str(ret).upper()
    if "NOT_LOGIN" in r or "NEED_LOGIN" in r or "LOGIN_EXPIRED" in r:
        return "登录态已失效，重新扫码：Windows -> run.cmd login / Linux -> python3 xianyu_login.py"
    if "TOKEN_EMPTY" in r or "TOKEN_EXPIRED" in r:
        return "签名 token 异常（一般会自动重试），持续出现多为账号被风控"
    if "ILLEGAL_ACCESS" in r:
        return "签名被拒：账号可能已被风控，建议把 interval_seconds 调大、减少关键词"
    if "RGV587" in r or "FORBID" in r or "RISK" in r or "BLOCK" in r:
        return "触发闲鱼风控，请放慢频率（interval_seconds 不低于 300）或换一个账号"
    if "NOT_FOUND" in r or "API_NOT_FOUND" in r:
        return "接口不存在了 —— 闲鱼大概率改版，需要更新请求参数"
    if "TOO_MANY" in r or "FREQ" in r or "LIMIT" in r:
        return "请求过于频繁，提高 interval_seconds"
    return ""


# ── 登录态 ──────────────────────────────────────────────────────────────────
class Session:
    """持有 CookieJar：登录 cookie 从 storage-state.json 载入，
    _m_h5_tk（签名 token）由接口域名首次响应下发，jar 自动收纳。"""

    def __init__(self, state_path: pathlib.Path):
        self.jar = cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )
        n = self._load_state(state_path)
        if n == 0:
            raise RuntimeError(f"登录态为空或无法解析: {state_path}")
        log(f"载入登录 cookie {n} 条")
        _warn_if_proxied()

    def _load_state(self, path: pathlib.Path) -> int:
        if not path.exists():
            raise RuntimeError(f"找不到登录态文件: {path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        n = 0
        for c in data.get("cookies", []):
            dom = c.get("domain", "")
            if not dom:
                continue
            try:
                self.jar.set_cookie(cookiejar.Cookie(
                    version=0, name=c["name"], value=c["value"],
                    port=None, port_specified=False,
                    domain=dom, domain_specified=True,
                    domain_initial_dot=dom.startswith("."),
                    path=c.get("path", "/"), path_specified=True,
                    secure=bool(c.get("secure", False)),
                    expires=c.get("expires"), discard=False,
                    comment=None, comment_url=None, rest={},
                ))
                n += 1
            except Exception:
                continue
        return n

    @property
    def token(self) -> str:
        for c in self.jar:
            if c.name == "_m_h5_tk":
                return c.value.split("_")[0]
        return ""

    def _raw(self, data: str) -> dict:
        t = str(int(time.time() * 1000))
        sign = hashlib.md5(
            f"{self.token}&{t}&{APP_KEY}&{data}".encode()
        ).hexdigest()
        qs = urllib.parse.urlencode({
            "jsv": "2.7.2", "appKey": APP_KEY, "t": t, "sign": sign,
            "v": "1.0", "type": "originaljson", "accountSite": "xianyu",
            "dataType": "json", "timeout": "20000", "api": API,
            "sessionOption": "AutoLoginOnly", "spm_cnt": "a21ybx.search.0.0",
        })
        req = urllib.request.Request(
            f"{ENDPOINT}?{qs}",
            data=urllib.parse.urlencode({"data": data}).encode(),
            method="POST",
        )
        for k, v in (("User-Agent", UA),
                     ("Content-Type", "application/x-www-form-urlencoded"),
                     ("Referer", "https://www.goofish.com/"),
                     ("Origin", "https://www.goofish.com"),
                     ("Accept", "application/json")):
            req.add_header(k, v)
        with self.opener.open(req, timeout=25) as r:
            return json.loads(r.read().decode("utf-8", "replace"))

    def search(self, keyword: str, page: int, rows: int) -> list[dict]:
        payload = json.dumps({
            "pageNumber": page, "keyword": keyword, "fromFilter": False,
            "rowsPerPage": rows,
            "sortField": "CREATE",          # 按最新发布降序
            "sortValue": "",
            "customDistance": "", "gps": "", "propValueStr": {},
            "customGps": "", "searchReqFromPage": "pcSearch",
            "extraFilterValue": "{}", "userPositionJson": "{}",
        }, ensure_ascii=False, separators=(",", ":"))

        res = self._raw(payload)
        # 首次没有 token：服务端会在响应里下发 _m_h5_tk，拿到后用新 token 重签一次
        # token 过期(ILLEGAL_ACCESS)同理，服务端会顺带刷新 cookie
        ret0 = str(res.get("ret", ""))
        if self.token and ("FAIL_SYS_TOKEN_EMPTY" in ret0
                           or "FAIL_SYS_ILLEGAL_ACCESS" in ret0):
            res = self._raw(payload)
        ret = str(res.get("ret", ""))
        if "SUCCESS" not in ret:
            extra = _ret_hint(ret)
            raise RuntimeError(
                f"接口返回异常: {ret} / {str(res.get('data', ''))[:120]}"
                + (f"  {extra}" if extra else "")
            )
        return (res.get("data") or {}).get("resultList") or []


# ── 解析 ────────────────────────────────────────────────────────────────────
def parse_item(node: dict) -> dict | None:
    item = (node.get("data") or {}).get("item") or {}
    main = item.get("main") or {}
    ex = main.get("exContent") or {}
    args = (main.get("clickParam") or {}).get("args") or {}

    iid = str(ex.get("itemId") or args.get("id") or "").strip()
    if not iid:
        return None
    if ex.get("isAliMaMaAD"):
        return None

    tags = []
    for group in (ex.get("fishTags") or {}).values():
        if isinstance(group, dict):
            for t in group.get("tagList") or []:
                c = ((t.get("data") or {}).get("content") or "").strip()
                if c:
                    tags.append(c)

    return {
        "id": iid,
        "title": (ex.get("title") or "").strip(),
        "price": price_text(ex),
        "price_num": price_num(ex),
        "area": (ex.get("area") or "").strip(),
        "seller": ((ex.get("detailParams") or {}).get("userNick") or "").strip(),
        "pic": (ex.get("picUrl") or "").strip(),
        "tags": tags[:5],
        "publish": int(args["publishTime"]) if str(args.get("publishTime", "")).isdigit() else None,
    }


def keep(item: dict, task: dict) -> bool:
    title = item["title"]
    for w in task.get("exclude") or []:
        if w and w in title:
            return False
    for w in task.get("include") or []:
        if w and w not in title:
            return False
    p = item["price_num"]
    if p is not None:
        lo, hi = task.get("min_price"), task.get("max_price")
        if lo is not None and p < float(lo):
            return False
        if hi is not None and p > float(hi):
            return False
    return True


# ── 状态 ────────────────────────────────────────────────────────────────────
class State:
    def __init__(self, path: pathlib.Path):
        self.path = path
        self.data = {"tasks": {}}
        if path.exists():
            try:
                self.data = json.loads(path.read_text(encoding="utf-8"))
                self.data.setdefault("tasks", {})
            except Exception:
                log("state.json 损坏，已重建")

    def save(self) -> None:
        self.path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    def bucket(self, keyword: str) -> dict:
        return self.data["tasks"].setdefault(keyword, {"items": [], "seen": []})


# ── RSS ─────────────────────────────────────────────────────────────────────
def build_rss(keyword: str, items: list[dict], self_url: str = "") -> str:
    title = f"闲鱼上新 · {keyword}"
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:atom="http://www.w3.org/2005/Atom">',
        "<channel>",
        f"<title>{escape(title)}</title>",
        f"<link>{escape(ITEM_URL)}</link>",
        f"<description>{escape(title)}</description>",
        "<language>zh-CN</language>",
        f"<lastBuildDate>{http_date(int(time.time() * 1000))}</lastBuildDate>",
    ]
    if self_url:
        out.append(f'<atom:link href="{escape(self_url)}" rel="self" '
                   'type="application/rss+xml"/>')
    for it in items:
        desc = [f"价格：{it['price'] or '面议'}"]
        if it["area"]:
            desc.append(f"地区：{it['area']}")
        if it["seller"]:
            desc.append(f"卖家：{it['seller']}")
        if it["tags"]:
            desc.append("标签：" + " / ".join(it["tags"]))
        html = "".join(
            f"<p>{escape(x)}</p>" for x in desc
        ) + (f'<p><img src="{escape(it["pic"])}"/></p>' if it["pic"] else "")
        out += [
            "<item>",
            f"<title>{escape(it['title'] or '(无标题)')}</title>",
            f"<link>{escape(ITEM_URL + it['id'])}</link>",
            f"<guid isPermaLink=\"false\">xianyu-{escape(it['id'])}</guid>",
            f"<pubDate>{http_date(it['publish'])}</pubDate>",
            f"<description>{escape(html)}</description>",
        ]
        if it["pic"]:
            out.append(f'<enclosure url="{escape(it["pic"])}" type="image/jpeg"/>')
        out.append("</item>")
    out += ["</channel>", "</rss>"]
    return "\n".join(out)


# ── 主流程 ──────────────────────────────────────────────────────────────────
def _md(items: list[dict]) -> str:
    return "\n\n".join(
        f"**{it['price'] or '面议'}** [{it['title'][:40]}]({ITEM_URL + it['id']})"
        + (f"  \n{it['area']}" if it["area"] else "")
        for it in items[:10]
    )


def run_once(cfg: dict, sess: Session, st: State, base_url: str = "") -> int:
    out_dir = BASE / cfg["output_dir"]
    out_dir.mkdir(exist_ok=True)
    total_new = 0
    lo, hi = (cfg.get("request_delay") or [1.5, 4.0])[:2]
    feed_size = int(cfg.get("feed_size", 100))

    for task in cfg.get("tasks", []):
        kw = task["keyword"]
        b = st.bucket(kw)
        seen = dict.fromkeys(b["seen"])   # 用 dict 当"有序集合"：必须保序，见下方截断
        pages = int(task.get("pages") or cfg.get("pages", 1))
        rows = int(task.get("rows_per_page") or cfg.get("rows_per_page", 30))

        fresh: list[dict] = []
        fetched = 0
        for pg in range(1, pages + 1):
            if pg > 1:
                time.sleep(random.uniform(lo, hi))
            try:
                raw = sess.search(kw, pg, rows)
            except Exception as e:
                log(f"  [{kw}] 第{pg}页抓取失败: {e}")
                continue
            fetched += len(raw)
            for node in raw:
                it = parse_item(node)
                if it and it["id"] not in seen and keep(it, task):
                    seen[it["id"]] = None
                    fresh.append(it)
            time.sleep(random.uniform(lo, hi))

        if fresh:
            b["items"] = fresh + b["items"]
            del b["items"][feed_size:]
            b["seen"] = list(seen)[-2000:]   # 依赖上面 dict 的插入顺序，
            # 保的是"最近见过的 2000 个 ID"。这里绝不能用 list(set(...))：
            # set 的迭代顺序受 PYTHONHASHSEED 影响每次都不同，会随机丢掉近期 ID，
            # 导致老商品被重新推进 RSS（表现为 FreshRSS 里"旧货反复回归"）。
            total_new += len(fresh)
            log(f"  [{kw}] 抓到 {fetched} 条，新增 {len(fresh)} 条")
            for it in fresh[:3]:
                log(f"      + {it['price']} {it['title'][:36]}")
            if len(fresh) >= 10:
                notify(f"闲鱼上新 {kw} +{len(fresh)}", _md(fresh),
                       bool(cfg.get("notify")))
        else:
            log(f"  [{kw}] 抓到 {fetched} 条，无新增")

        slug = _safe_filename(task_slug(task))
        self_url = feed_url(slug, base_url) if base_url else ""
        (out_dir / f"{slug}.xml").write_text(
            build_rss(kw, b["items"], self_url), encoding="utf-8"
        )

    st.save()
    log(f"本轮新增 {total_new} 条，feed 已写入 {out_dir}")
    return total_new


def _lan_ip() -> str:
    """取本机局域网 IP（FreshRSS 在别的机器/容器里时要用这个）。"""
    import socket

    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("223.5.5.5", 80))  # 只用来让系统选出出口网卡，不发包
            return str(s.getsockname()[0])
        finally:
            s.close()
    except Exception:
        return ""


def print_feed_links(cfg: dict, port: int) -> None:
    """启动时把每个关键词的订阅地址打清楚，避免手抄 URL 出错。"""
    ip = _lan_ip()
    log(f"RSS 服务已启动，打开 http://127.0.0.1:{port}/ 可看到全部订阅地址")
    for t in cfg.get("tasks", []):
        kw = str(t.get("keyword", ""))
        href = feed_url(_safe_filename(task_slug(t)))
        log(f"  [{kw}]")
        log(f"      http://127.0.0.1:{port}/{href}")
        if ip:
            log(f"      http://{ip}:{port}/{href}   <- FreshRSS 用这条")
    if ip:
        log("  本机预览用第一条；FreshRSS / 局域网设备用带内网 IP 的那条。")


def build_index(cfg: dict) -> str:
    """GET / 返回一个干净的订阅列表（链接只做一次百分号编码）。"""
    rows = []
    for t in cfg.get("tasks", []):
        kw = escape(str(t.get("keyword", "")))
        slug = _safe_filename(task_slug(t))
        href = feed_url(slug)
        rows.append(
            f"      <li><b>{kw}</b> <code>{href}</code> "
            f'<a class="btn" href="/{href}">打开</a></li>'
        )
    if not rows:
        rows.append("      <li>config.json 里还没有任何任务</li>")
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>闲鱼上新监控 - 订阅列表</title>
  <style>
    body{{font:15px/1.7 system-ui,-apple-system,"Segoe UI",sans-serif;
         max-width:760px;margin:48px auto;padding:0 20px;color:#222}}
    code{{background:#f4f5f7;padding:2px 6px;border-radius:4px;font-size:13px}}
    li{{margin:10px 0;list-style:none;border-bottom:1px solid #eee;padding-bottom:8px}}
    .btn{{margin-left:10px;color:#07c;text-decoration:none}}
    .tip{{color:#888;font-size:13px;margin-top:24px}}
  </style>
</head>
<body>
  <h1>闲鱼上新监控</h1>
  <p>把下面的地址填进 FreshRSS（复制到 navigation bar 的订阅框里）：</p>
  <ul>
{chr(10).join(rows)}
  </ul>
  <p class="tip">FreshRSS 若跑在 Docker 里，请把 127.0.0.1 换成宿主机的局域网 IP。<br>
  想给 feed 换个英文短名，在 config.json 的对应任务里加一行 <code>"name": "xxx"</code>。</p>
</body>
</html>
"""


def cmd_serve(cfg: dict) -> None:
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

    out_dir = (BASE / cfg["output_dir"]).resolve()
    out_dir.mkdir(exist_ok=True)

    class H(SimpleHTTPRequestHandler):
        def translate_path(self, path: str) -> str:
            # 浏览器/阅读器传来的路径是百分号编码的，这里解出真实文件名一次即可
            p = urllib.parse.unquote(urllib.parse.urlparse(path).path).lstrip("/")
            target = (out_dir / p).resolve() if p else out_dir
            try:
                target.relative_to(out_dir)
            except ValueError:  # 越界访问（../）一律挡回根目录
                return str(out_dir)
            return str(target) if p and not p.endswith("/") else str(out_dir)

        def do_GET(self) -> None:
            if urllib.parse.urlparse(self.path).path.strip("/") == "":
                self._index()
                return
            super().do_GET()

        def _index(self) -> None:
            # Cache-Control 由 end_headers() 统一发送，这里不要再发一次
            body = build_index(cfg).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def guess_type(self, path: str) -> str:
            # 从源头给正确 MIME，避免 SimpleHTTPRequestHandler 再补一个 text/xml
            if str(path).endswith(".xml"):
                return "application/rss+xml; charset=utf-8"
            return super().guess_type(path)

        def end_headers(self):
            self.send_header("Cache-Control", "no-cache")
            super().end_headers()

        def log_message(self, *a):
            pass

    host, port = cfg.get("serve_host", "0.0.0.0"), int(cfg.get("serve_port", 8899))
    srv = ThreadingHTTPServer((host, port), H)
    print_feed_links(cfg, port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("服务已停止")


def cmd_all(cfg: dict, sess: Session, st: State) -> None:
    """一条命令常驻：后台线程抓取 + 主线程提供 RSS。"""
    import threading

    interval = int(cfg.get("interval_seconds", 300))
    base = f"http://127.0.0.1:{cfg.get('serve_port', 8899)}"

    def worker():
        while True:
            try:
                run_once(cfg, sess, st, base_url=base)
            except Exception as e:
                log(f"[ERROR] 本轮异常: {e}")
            time.sleep(interval)

    threading.Thread(target=worker, daemon=True).start()
    log(f"抓取线程已启动，间隔 {interval}s")
    cmd_serve(cfg)


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "all"
    cfg = load_config(DEFAULT_CONFIG)

    if cmd == "serve":
        cmd_serve(cfg)
        return 0

    state_path = BASE / cfg["state_file"]
    cookie_path = pathlib.Path(cfg["cookie_state"])
    if not cookie_path.is_absolute():
        cookie_path = BASE / cookie_path

    try:
        sess = Session(cookie_path)
    except Exception as e:
        log(f"[ERROR] {e}")
        log("提示：执行登录脚本扫码，会自动生成 storage-state.json：")
        log("      Windows -> run.cmd login ")
        log("      Linux   -> python3 xianyu_login.py")
        return 1

    st = State(state_path)

    if cmd == "check":
        try:
            r = sess.search(cfg["tasks"][0]["keyword"], 1, 3)
            log(f"接口正常，返回 {len(r)} 条（登录态有效）")
            return 0
        except Exception as e:
            log(f"[ERROR] {e}")
            return 1

    lock = FileLock(BASE / ".lock")
    if not lock.acquire():
        log("上一轮仍在执行，本轮跳过（避免并发翻倍请求量触发风控）")
        return 0

    try:
        if cmd == "once":
            run_once(cfg, sess, st)
            return 0

        if cmd == "loop":
            interval = int(cfg.get("interval_seconds", 300))
            base = f"http://127.0.0.1:{cfg.get('serve_port', 8899)}"
            log(f"常驻模式启动，间隔 {interval}s，Ctrl+C 退出")
            while True:
                try:
                    run_once(cfg, sess, st, base_url=base)
                except Exception as e:
                    log(f"[ERROR] 本轮异常: {e}")
                time.sleep(interval)
            return 0

        if cmd == "all":
            cmd_all(cfg, sess, st)
            return 0

        print(__doc__)
        return 1
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
