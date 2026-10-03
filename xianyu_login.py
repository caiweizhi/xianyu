#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""闲鱼扫码登录 -> storage-state.json

登录流程对齐 **xianyu-monitor-master 的 webbind.py**（已实测能拿到可用登录态）：
打开真实 Chromium（headless）登录页 -> 拦截二维码接口 -> 手机扫码 ->
确认后等浏览器上下文里出现登录标记 cookie -> 存 storage-state.json。

为什么必须用浏览器（纯 HTTP 不行，别再试了）
------------------------------------------
实测两种结果对照：

| 做法 | 拿到的 cookie | 结果 |
|---|---|---|
| 纯 HTTP：登录页 + generate + query + 收尾跳转 | 7 条（XSRF-TOKEN/_samesite_flag_/cookie2/t/_tb_token_/cna/sca） | 抓搜索 `RGV587_ERROR::SM::哎哟喂,被挤爆啦` |
| 浏览器（参考项目同款） | 19 条（含 unb/sgcookie/tracknick/csg/tfstk/xlly_s/KLNotice） | `SUCCESS::调用成功` |

差的这 12 条里 `unb`/`sgcookie`/`tracknick`/`csg`/`tfstk`/`xlly_s`/`KLNotice`
**不是 Set-Cookie 下发的，是 www.goofish.com 页面里 JS 算出来写进去的**：
登录页自身的 `havana-nlogin/index.js` 里根本没出现过这几个名字，
纯 HTTP 请求到 HTML 也不会执行脚本。只有真实浏览器跑完登录后的跳转链才补齐。

用法
----
    python xianyu_login.py                  # 浏览器扫码登录（推荐）
    python xianyu_login.py --http           # 退回纯 HTTP 流程（大概率残缺）
    python xianyu_login.py --headful        # 有头窗口，方便看页面
    python xianyu_login.py --no-verify      # 跳过真接口校验
    python xianyu_login.py --no-merge       # 不要拿旧登录态补洞
    python xianyu_login.py --keyword macmini

依赖：qrcode / pillow（渲染二维码）；--http 模式只要标准库。
浏览器模式需要 playwright（`pip install playwright && playwright install chromium`）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import pathlib
import shutil
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = pathlib.Path(__file__).resolve().parent
STATE_FILE = BASE / "storage-state.json"
BACKUP_FILE = BASE / "storage-state.bak.json"
QR_PNG = BASE / "login-qr.png"
TMP_STATE = BASE / "storage-state.new.json"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 与 xianyu-monitor-master/backend/xianyu_monitor/webbind.py 完全一致
PASSPORT_LOGIN_URL = (
    "https://passport.goofish.com/mini_login.htm"
    "?lang=zh_cn&appName=xianyu&appEntrance=web&styleType=vertical"
    "&bizParams=&notLoadSsoView=false&notKeepLogin=false&isMobile=false"
    "&qrCodeFirst=true&stie=77"
)
QR_LOGIN_URL = "https://www.goofish.com/"
QR_GENERATE_ENDPOINT = "/newlogin/qrcode/generate.do"
QR_QUERY_ENDPOINT = "/newlogin/qrcode/query.do"

QR_NEW = "NEW"
QR_SCANNED = "SCANED"
QR_CONFIRMED = "CONFIRMED"
QR_CANCELED = "CANCELED"
QR_EXPIRED = "EXPIRED"

# 判断「登录态真的落地了」的标记（参考项目用的是同一套 AUTH_COOKIE_NAMES）
AUTH_COOKIE_NAMES = {
    "cookie1", "cookie17", "lgc", "_nk_",
    "sgcookie", "unb", "tracknick", "uc1",
}

# 身份 cookie：前 3 个是「能抓搜索」的硬门槛，后 4 个有则风控评分更低。
# 实测只缺后 4 个时接口还能通，所以判据只看前 3 个，免得正常登录被误报成失败。
NEEDED_IDENTITY = ("unb", "sgcookie", "tracknick")
NICE_IDENTITY = ("csg", "tfstk", "xlly_s", "KLNotice")

QR_TIMEOUT = 180           # 单个二维码有效期
QR_MAX_REFRESH = 6         # 最多刷新几次
TOTAL_TIMEOUT = 300        # 一次登录的等待上限


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ───────────────────────────────────────────────────────────────────────────
# 二维码渲染
# ───────────────────────────────────────────────────────────────────────────
def render_terminal_qr(content: str) -> bool:
    """终端里画二维码（半块字符，一次两行）。画不出来返回 False。"""
    try:
        import qrcode
    except ImportError:
        return False
    try:
        qr = qrcode.QRCode(border=4)
        qr.add_data(content)
        qr.make(fit=True)
        matrix = qr.get_matrix()
    except Exception:                                   # noqa: BLE001
        return False

    size = len(matrix)
    lines = []
    for row in range(0, size, 2):
        line = ""
        for col in range(size):
            top = matrix[row][col]
            bottom = matrix[row + 1][col] if row + 1 < size else False
            line += "█" if (top and bottom) else "▀" if top else "▄" if bottom else " "
        lines.append(line)
    print("\n" + "\n".join(lines) + "\n", flush=True)
    return True


def save_qr_png(content: str) -> pathlib.Path | None:
    """存一张 PNG 并尽量自动打开。失败返回 None（终端里已经有二维码了）。"""
    try:
        import qrcode
        qr = qrcode.QRCode(border=1)
        qr.add_data(content)
        qr.make(fit=True)
        qr.make_image().save(QR_PNG)
    except Exception as e:                              # noqa: BLE001
        log(f"(PNG 保存失败，可忽略：{e})")
        return None
    log(f"二维码图片已保存: {QR_PNG}")
    try:
        if sys.platform == "win32":
            os.startfile(QR_PNG)                        # noqa: S606
        elif sys.platform == "darwin":
            os.system(f'open "{QR_PNG}"')
        else:
            os.system(f'xdg-open "{QR_PNG}"')
    except Exception:                                   # noqa: BLE001
        pass
    return QR_PNG


def show_qr(content: str) -> None:
    """终端画 + 存 PNG 自动打开；二维码库缺了就只打印链接。"""
    shown = render_terminal_qr(content)
    if not shown:
        try:
            import qrcode  # noqa: F401
        except ImportError:
            print("\n  未安装 qrcode，终端画不了二维码。")
            print(f"  请自行用下面这串内容生成二维码并用闲鱼 App 扫码：\n\n  {content}\n")
            print("  或执行：  pip install qrcode pillow\n")
            save_qr_png(content)
            return
    save_qr_png(content)


# ───────────────────────────────────────────────────────────────────────────
# 浏览器登录（默认，与 xianyu-monitor-master 同款）
# ───────────────────────────────────────────────────────────────────────────
def _qr_content_from(payload: dict) -> str:
    content = payload.get("content")
    if not isinstance(content, dict):
        return ""
    data = content.get("data")
    if not isinstance(data, dict):
        return ""
    return str(data.get("codeContent", "")).strip()


def _qr_status_from(payload: dict) -> str:
    content = payload.get("content")
    if not isinstance(content, dict):
        return ""
    data = content.get("data")
    if not isinstance(data, dict):
        return ""
    return str(data.get("qrCodeStatus", "")).strip().upper()


def has_login_markers(cookies) -> bool:
    for c in cookies or []:
        if not isinstance(c, dict):
            continue
        if "goofish.com" in str(c.get("domain", "")) and \
                str(c.get("name", "")) in AUTH_COOKIE_NAMES:
            return True
    return False


STATUS_TIP = {
    QR_NEW: "等待扫码...",
    QR_SCANNED: "已扫码，请在闲鱼 App 里点「确认登录」",
    QR_CONFIRMED: "已确认，等待登录态落地...",
    QR_EXPIRED: "二维码已过期，刷新中...",
    QR_CANCELED: "已取消，刷新中...",
}


class BrowserUnavailable(RuntimeError):
    """playwright 缺失或 Chromium 起不来（跟扫码没关系，可以退回 HTTP 流程）。"""


async def _browser_login(*, timeout: int, headful: bool) -> tuple[str, list[str]]:
    """Chromium 无头登录。返回 (storage_state 文本, 落地后的 cookie 名列表)。"""
    try:
        from playwright.async_api import async_playwright
    except ImportError as e:
        raise BrowserUnavailable(
            "缺少 playwright。浏览器登录需要它：\n"
            "    pip install playwright && playwright install chromium\n"
            f"（原始错误：{e}）") from e

    watch = {"last": "", "confirmed": False}

    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(headless=not headful)
        except Exception as e:                          # noqa: BLE001
            raise BrowserUnavailable(f"Chromium 启动失败：{e}") from e
        context = await browser.new_context(locale="zh-CN",
                                            timezone_id="Asia/Shanghai",
                                            user_agent=UA)
        page = await context.new_page()

        async def _handle(response) -> None:
            if QR_QUERY_ENDPOINT not in response.url:
                return
            try:
                payload = await response.json()
            except Exception:                           # noqa: BLE001
                return
            status = _qr_status_from(payload)
            if not status or status == watch["last"]:
                return
            watch["last"] = status
            if status == QR_CONFIRMED:
                watch["confirmed"] = True

        page.on("response", lambda r: asyncio.ensure_future(_handle(r)))

        try:
            # 打开登录页，拦截它自己发的二维码请求
            async with page.expect_response(
                    lambda r: QR_GENERATE_ENDPOINT in r.url,
                    timeout=30_000) as qr_info:
                await page.goto(PASSPORT_LOGIN_URL, wait_until="domcontentloaded")

            payload = await (await qr_info.value).json()
            content = _qr_content_from(payload)
            if not content:
                raise RuntimeError(f"未能从二维码接口提取内容: {payload}")
            show_qr(content)
            log("请用「闲鱼 App」扫上面的二维码，并在手机上点确认登录")

            deadline = time.time() + timeout
            while time.time() < deadline:
                if watch["last"] in (QR_CANCELED, QR_EXPIRED):
                    break
                if watch["confirmed"]:
                    break
                await asyncio.sleep(1)
            if not watch["confirmed"]:
                raise TimeoutError(f"已出码但 {timeout}s 内没等到确认（最后状态="
                                   f"{watch['last'] or '无'}），请重跑再扫")

            # 确认后等登录 cookie 落地（浏览器完成跳转链，JS 补齐身份 cookie）
            cookie_deadline = time.time() + 30
            cookies = []
            while time.time() < cookie_deadline:
                cookies = await context.cookies([QR_LOGIN_URL,
                                                 "https://passport.goofish.com"])
                if has_login_markers(cookies):
                    break
                await asyncio.sleep(1)
            if not has_login_markers(cookies):
                raise RuntimeError("确认后仍未拿到登录 cookie，登录态是残的")

            state = await context.storage_state()
            names = sorted({str(c.get("name")) for c in state.get("cookies", [])})
            return json.dumps(state, ensure_ascii=False), names
        finally:
            await context.close()
            await browser.close()


# ───────────────────────────────────────────────────────────────────────────
# 纯 HTTP 登录（兜底，拿到的大概率是残缺登录态）
# ───────────────────────────────────────────────────────────────────────────
class HttpLogin:
    LOGIN_PAGE = "https://passport.goofish.com/mini_login.htm"
    GENERATE = ("https://passport.goofish.com/newlogin/qrcode/generate.do"
                "?appName=xianyu&fromSite=77")
    QUERY = "https://passport.goofish.com/newlogin/qrcode/query.do"
    JS_VERSION = "0.10.37"

    def __init__(self):
        import http.cookiejar as cj
        import urllib.request as ur
        self.jar = cj.CookieJar()
        self.opener = ur.build_opener(ur.HTTPCookieProcessor(self.jar))

    def request(self, url, data=None, referer=None, xhr=True, timeout=25):
        import urllib.request as ur
        referer = referer or self.LOGIN_PAGE
        req = ur.Request(url, data=data, method="POST" if data else "GET")
        req.add_header("User-Agent", UA)
        req.add_header("Referer", referer)
        req.add_header("Accept-Language", "zh-CN,zh;q=0.9")
        if xhr:
            req.add_header("X-Requested-With", "XMLHttpRequest")
            req.add_header("Accept", "application/json, text/plain, */*")
        else:
            req.add_header("Accept",
                           "text/html,application/xhtml+xml,*/*;q=0.8")
        if data:
            req.add_header("Content-Type", "application/x-www-form-urlencoded")
        with self.opener.open(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")

    def page(self, url, referer=None, timeout=25):
        try:
            return self.request(url, referer=referer, timeout=timeout, xhr=False)
        except urllib.error.HTTPError:
            return url
        except Exception as e:                          # noqa: BLE001
            log(f"(收尾请求异常，可忽略: {e})")
            return url

    def names(self):
        return {c.name for c in self.jar}

    def generate(self):
        payload = json.loads(self.request(self.GENERATE))
        d = (payload.get("content") or {}).get("data") or {}
        if not d.get("codeContent"):
            raise RuntimeError(f"接口未返回二维码内容: {payload}")
        return d

    def query(self, t, ck):
        params = {
            "appName": "xianyu", "fromSite": "77",
            "t": t, "ck": ck, "ua": UA,
            "umidToken": "", "umidTag": "NOT_INIT",
            "navlanguage": "zh-CN", "navUserAgent": UA, "navPlatform": "Win32",
            "isIframe": "false", "banThirdPartyCookie": "false",
            "documentReferer": self.LOGIN_PAGE, "defaultView": "default",
            "jsVersion": self.JS_VERSION,
        }
        payload = json.loads(self.request(
            self.QUERY, data=urllib.parse.urlencode(params).encode()))
        d = (payload.get("content") or {}).get("data") or {}
        return str(d.get("qrCodeStatus") or ""), payload

    def handle_confirmed(self, data):
        before = self.names()
        for key in ("asyncUrls", "miniVsts", "miniLogouts"):
            for u in (data.get(key) or []) or []:
                if isinstance(u, str) and u.startswith("http"):
                    self.page(u, referer="https://passport.goofish.com/")
                    time.sleep(1)
        for key in ("redirectUrl", "parentRedirectUrl", "iframeRedirectUrl",
                    "url", "targetUrl", "loginUrl", "miniLoginUrl"):
            v = data.get(key)
            if isinstance(v, str) and v.startswith("http"):
                self.page(v, referer="https://passport.goofish.com/")
                time.sleep(1.5)
        for target in ("https://www.goofish.com/", "https://www.goofish.com/personal?from=h5"):
            self.page(target, referer="https://passport.goofish.com/")
            time.sleep(1.5)
        gained = sorted(self.names() - before)
        if gained:
            log(f"补齐 cookie: {gained}")

    def dump(self):
        return {
            "cookies": [
                {"name": c.name, "value": c.value, "domain": c.domain,
                 "path": c.path or "/", "expires": c.expires,
                 "secure": bool(c.secure), "httpOnly": False,
                 "sameSite": "Lax"}
                for c in self.jar],
            "origins": [],
        }


def _http_login(*, timeout: int):
    sess = HttpLogin()
    try:
        sess.page(sess.LOGIN_PAGE)
    except Exception as e:                              # noqa: BLE001
        log(f"(登录页访问异常，可忽略: {e})")
    data = sess.generate()
    t, ck, content = data["t"], data["ck"], data["codeContent"]
    show_qr(content)
    log("请用「闲鱼 App」扫上面的二维码，并在手机上点确认登录")

    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        time.sleep(2)
        try:
            status, payload = sess.query(t, ck)
        except Exception as e:                          # noqa: BLE001
            log(f"轮询失败，重试: {e}")
            continue
        if status != last:
            last = status
            log(STATUS_TIP.get(status, status))
        if status in (QR_EXPIRED, QR_CANCELED):
            break
        if status == QR_CONFIRMED:
            sess.handle_confirmed((payload.get("content") or {}).get("data") or {})
            return json.dumps(sess.dump(), ensure_ascii=False), sorted(sess.names())
    raise TimeoutError("纯 HTTP 登录超时（没等到确认）")


# ───────────────────────────────────────────────────────────────────────────
# 落盘 / 校验 / 补洞
# ───────────────────────────────────────────────────────────────────────────
def backup_old_state() -> None:
    if STATE_FILE.exists():
        try:
            shutil.copy2(STATE_FILE, BACKUP_FILE)
            log(f"旧登录态已备份 -> {BACKUP_FILE}")
        except Exception as e:                          # noqa: BLE001
            log(f"(备份失败，可忽略: {e})")


def names_in(state_text: str) -> set[str]:
    try:
        data = json.loads(state_text)
    except Exception:                                   # noqa: BLE001
        return set()
    return {str(c.get("name")) for c in data.get("cookies", [])}


def missing_identity(state_text: str) -> list[str]:
    have = names_in(state_text)
    return [n for n in NEEDED_IDENTITY if n not in have]


def missing_nice(state_text: str) -> list[str]:
    have = names_in(state_text)
    return [n for n in NICE_IDENTITY if n not in have]


def merge_state(target_text: str, fill_text: str) -> tuple[str, list[str]]:
    """用旧登录态给新状态补洞，返回 (合并后文本, 补进来的 cookie 名)。"""
    try:
        target = json.loads(target_text)
        fill = json.loads(fill_text)
    except Exception as e:                              # noqa: BLE001
        log(f"(合并失败，可忽略: {e})")
        return target_text, []
    have = {(c.get("name"), c.get("domain")) for c in target.get("cookies", [])}
    added = []
    for c in fill.get("cookies", []):
        key = (c.get("name"), c.get("domain"))
        if key in have:
            continue
        target.setdefault("cookies", []).append(c)
        have.add(key)
        added.append(str(c.get("name")))
    return json.dumps(target, ensure_ascii=False, indent=1), added


def verify(state_text: str, keyword: str) -> tuple[bool, str]:
    """拿真接口跑一次搜索，确认这份登录态能用。"""
    sys.path.insert(0, str(BASE))
    tmp = BASE / "_verify_state.json"
    tmp.write_text(state_text, encoding="utf-8")
    try:
        import xianyu_rss as X
        s = X.Session(tmp)
        rows = s.search(keyword, 1, 20)
        if not rows:
            return False, "接口通了但没搜到商品（可能被风控拦成空结果）"
        return True, f"搜索「{keyword}」返回 {len(rows)} 条商品"
    except Exception as e:                              # noqa: BLE001
        return False, str(e)[:160]
    finally:
        try:
            tmp.unlink()
        except Exception:                               # noqa: BLE001
            pass


def first_keyword() -> str:
    try:
        cfg = json.loads((BASE / "config.json").read_text(encoding="utf-8"))
        for t in cfg.get("tasks", []):
            if t.get("keyword"):
                return str(t["keyword"])
    except Exception:                                   # noqa: BLE001
        pass
    return "macmini"


LAUNCHER_COMMANDS = {"login", "all", "once", "loop", "serve", "check", "diag"}


def strip_launcher_args() -> None:
    """把外层启动器（run.cmd / 旧版 run.cmd）误传的子命令名摘掉。

    批处理里 shift 不会改变 %*，历史上 `run.cmd login` 会把 login 原样
    拼到本脚本上，导致 argparse 报 unrecognized arguments。这里容错处理。
    """
    kept = []
    for a in sys.argv[1:]:
        if a in LAUNCHER_COMMANDS:
            print(f"[提示] 已忽略启动器参数 {a!r}（本脚本直接跑登录）")
            continue
        kept.append(a)
    sys.argv[:] = [sys.argv[0]] + kept


def main() -> int:
    strip_launcher_args()
    ap = argparse.ArgumentParser(description="闲鱼扫码登录")
    ap.add_argument("--http", action="store_true",
                    help="用纯 HTTP 流程（不启浏览器），拿到的登录态大概率残缺")
    ap.add_argument("--headful", action="store_true",
                    help="浏览器有头模式，方便看页面（调试用）")
    ap.add_argument("--no-verify", action="store_true", help="跳过真接口校验")
    ap.add_argument("--no-merge", action="store_true",
                    help="不要拿旧登录态补缺失的 cookie")
    ap.add_argument("--keyword", default=first_keyword())
    ap.add_argument("--timeout", type=int, default=TOTAL_TIMEOUT)
    args = ap.parse_args()

    print("=" * 58)
    print("  闲鱼扫码登录" + ("（纯 HTTP 兜底流程）" if args.http else "（浏览器流程）"))
    print(f"  登录态将写入: {STATE_FILE}")
    print("=" * 58)

    backup_old_state()
    old_text = STATE_FILE.read_text(encoding="utf-8") if STATE_FILE.exists() else ""
    fill_text = BACKUP_FILE.read_text(encoding="utf-8") if BACKUP_FILE.exists() else old_text

    # ── 1. 拿登录态 ──────────────────────────────────────────────────────
    if args.http:
        state_text, names = _http_login(timeout=args.timeout)
    else:
        try:
            state_text, names = asyncio.run(
                _browser_login(timeout=args.timeout, headful=args.headful))
        except TimeoutError as e:
            log(f"[取消] {e}")
            log("       旧登录态仍完好（未被覆盖），可直接 python3 xianyu_rss.py once")
            return 1
        except BrowserUnavailable as e:
            log(f"[WARN] 浏览器登录不可用：{e}")
            log("       退回纯 HTTP 流程，结果大概率只有 7 条 cookie 并报 RGV587；")
            log("       建议装好 playwright 后重跑：")
            log("         pip install playwright && playwright install chromium")
            try:
                confirm = input("仍要继续纯 HTTP 登录吗? [y/N] ").strip().lower()
            except EOFError:
                return 1
            if confirm != "y":
                return 1
            state_text, names = _http_login(timeout=args.timeout)
    log(f"登录态共 {len(names)} 条 cookie: {names}")

    # ── 2. 缺身份 cookie 就用旧登录态补洞 ─────────────────────────────────
    miss = missing_identity(state_text)
    if miss and fill_text and not args.no_merge:
        state_text, added = merge_state(state_text, fill_text)
        if added:
            miss = missing_identity(state_text)
            log(f"用旧登录态补齐: {added}" + (f"  仍缺: {miss}" if miss else ""))
    elif miss:
        log(f"[WARN] 缺身份 cookie: {miss}（这些只能由浏览器 JS 写入，纯 HTTP 拿不到）")

    # ── 3. 校验 ──────────────────────────────────────────────────────────
    ok, msg = (True, "已跳过校验")
    if not args.no_verify:
        ok, msg = verify(state_text, args.keyword)
        log(f"登录态校验: {msg}")

    # ── 4. 落盘 ──────────────────────────────────────────────────────────
    STATE_FILE.write_text(state_text, encoding="utf-8")
    n = len(names_in(state_text))
    log(f"登录态已保存 {n} 条 cookie -> {STATE_FILE}")

    still_miss = missing_identity(state_text)
    nice_miss = missing_nice(state_text)
    if nice_miss:
        log(f"[提示] 风控加分项未拿到: {nice_miss}（不影响抓，有更好）")
    if still_miss:
        log("[WARN] 仍缺身份 cookie（浏览器模式下不该出现，多半是这次登录被风控打断）: "
            f"{still_miss}")
        log("       这份登录态很可能搜不了：先 python3 diag.py 确认，不行就把旧登录态拷回来。")
        return 1
    log("核心身份凭证齐全（unb / sgcookie / tracknick）")
    if not ok:
        log(f"[提示] 校验没过（{msg}）：账号可能刚跑过高频请求，处于风控窗口；"
            f"等几分钟再跑 python3 diag.py / xianyu_rss.py once 通常就恢复了。")
    print()
    log("下一步：python3 diag.py 确认，再 python3 xianyu_rss.py once")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已取消")
        sys.exit(1)
