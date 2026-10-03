#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""闲鱼扫码登录 -> storage-state.json

纯 HTTP 实现，不启动浏览器。流程（接口来自 passport.goofish.com 登录页自身）：
  1) GET  /newlogin/qrcode/generate.do?appName=xianyu&fromSite=77
     -> content.data.{t, ck, codeContent}   codeContent 就是要编进二维码的 URL
  2) 把 codeContent 渲染成二维码（终端 ASCII，可选存 PNG）
  3) POST /newlogin/qrcode/query.do   body: appName=xianyu&fromSite=77&t=<t>&ck=<ck>
     -> qrCodeStatus: NEW / SCANED / CONFIRMED / EXPIRED / CANCELED
  4) CONFIRMED 后访问 qrcodeCheck.htm?lgToken=... 落地 Cookie，再回 www.goofish.com 收尾
  5) 写 storage-state.json（Playwright 格式，xianyu_rss.py 直接可读）

依赖：
  运行时只用标准库。
  渲染二维码需要 qrcode（`pip install qrcode`），存 PNG 还需要 pillow。
  两者缺失时脚本会退化：只打印 codeContent，让你自己用任意工具生成二维码。

用法：
  python xianyu_login.py
"""

from __future__ import annotations

import http.cookiejar as cookiejar
import json
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = pathlib.Path(__file__).resolve().parent
STATE_FILE = BASE / "storage-state.json"
QR_PNG = BASE / "login-qr.png"

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
PASSPORT = "https://passport.goofish.com"
GENERATE = PASSPORT + "/newlogin/qrcode/generate.do?appName=xianyu&fromSite=77"
QUERY = PASSPORT + "/newlogin/qrcode/query.do"
QR_CHECK = PASSPORT + "/qrcodeCheck.htm"
REFERER = PASSPORT + "/mini_login.htm"

LOGIN_MARKERS = {"unb", "cookie2", "sgcookie", "cookie17", "lgc", "_nk_"}
POLL_INTERVAL = 2.0
QR_TIMEOUT = 180          # 单个二维码有效期
TOTAL_TIMEOUT = 420       # 含多次过期刷新


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


class Login:
    def __init__(self):
        self.jar = cookiejar.CookieJar()
        self.op = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar))

    def fetch(self, url: str, data: bytes | None = None,
              referer: str = REFERER, timeout: int = 20) -> str:
        req = urllib.request.Request(url, data=data,
                                     method="POST" if data else "GET")
        req.add_header("User-Agent", UA)
        req.add_header("Referer", referer)
        req.add_header("Accept", "application/json, text/plain, */*")
        req.add_header("X-Requested-With", "XMLHttpRequest")
        if data:
            req.add_header("Content-Type",
                           "application/x-www-form-urlencoded")
        with self.op.open(req, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")

    def generate(self) -> dict:
        payload = json.loads(self.fetch(GENERATE))
        d = (payload.get("content") or {}).get("data") or {}
        if not d.get("codeContent"):
            raise RuntimeError(f"接口未返回二维码内容: {payload}")
        return d

    def query(self, t, ck) -> tuple[str, dict]:
        body = urllib.parse.urlencode(
            {"appName": "xianyu", "fromSite": "77", "t": t, "ck": ck}
        ).encode()
        payload = json.loads(self.fetch(QUERY, data=body))
        d = (payload.get("content") or {}).get("data") or {}
        return str(d.get("qrCodeStatus") or ""), payload

    # ── 二维码渲染 ──────────────────────────────────────────────────────────
    @staticmethod
    def show_qr(content: str) -> None:
        try:
            import qrcode
        except ImportError:
            print("\n  未安装 qrcode，无法在此渲染二维码。")
            print(f"  请自行用下面这串内容生成二维码并用闲鱼 App 扫码：\n\n  {content}\n")
            print("  或执行：  pip install qrcode pillow\n")
            return

        qr = qrcode.QRCode(border=1)
        qr.add_data(content)
        print()
        try:
            qr.print_ascii(invert=True)
        except Exception:
            print("  (终端二维码渲染失败，已改为保存 PNG)")
        print()

        try:  # 顺手存一份 PNG，Windows 下自动用看图软件打开
            img = qr.make_image()
            img.save(QR_PNG)
            log(f"二维码图片已保存: {QR_PNG}")
            if sys.platform == "win32":
                try:
                    import os
                    os.startfile(QR_PNG)
                except Exception:
                    pass
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(QR_PNG)])
            else:
                subprocess.Popen(["xdg-open", str(QR_PNG)])
        except Exception as e:
            log(f"(PNG 保存/打开失败，可忽略：{e})")

    # ── 登录后收尾 ──────────────────────────────────────────────────────────
    def finish_login(self, lg_token: str) -> None:
        """确认之后：访问 qrcodeCheck.htm 让服务端下发会话 Cookie，再回访首页收尾。"""
        url = f"{QR_CHECK}?lgToken={lg_token}&_from=havana"
        for target, ref in ((url, REFERER),
                            ("https://www.goofish.com/", PASSPORT + "/")):
            try:
                self.fetch(target, referer=ref, timeout=25)
            except urllib.error.HTTPError:
                pass  # 302/403 都会顺带种 cookie，忽略即可
            except Exception as e:
                log(f"(收尾请求异常，可忽略: {e})")
            time.sleep(1)

    def has_login(self) -> bool:
        return bool(LOGIN_MARKERS & {c.name for c in self.jar})

    def save(self) -> int:
        cookies = []
        for c in self.jar:
            cookies.append({
                "name": c.name, "value": c.value,
                "domain": c.domain, "path": c.path or "/",
                "expires": c.expires, "secure": bool(c.secure),
                "httpOnly": False, "sameSite": "Lax",
            })
        STATE_FILE.write_text(
            json.dumps({"cookies": cookies, "origins": []},
                       ensure_ascii=False, indent=1), encoding="utf-8")
        return len(cookies)


def main() -> int:
    print("=" * 56)
    print("  闲鱼扫码登录（纯 HTTP，不启浏览器）")
    print(f"  登录态将写入: {STATE_FILE}")
    print("=" * 56)

    sess = Login()
    deadline = time.time() + TOTAL_TIMEOUT
    last_status = ""

    while time.time() < deadline:
        data = sess.generate()
        t, ck, content = data["t"], data["ck"], data["codeContent"]
        lg_token = urllib.parse.parse_qs(
            urllib.parse.urlparse(content).query).get("lgToken", [""])[0]
        if not lg_token:
            raise RuntimeError(f"无法从二维码内容解析 lgToken: {content}")

        print()
        log("二维码已就绪，请用「闲鱼 App」扫码并在手机上确认登录")
        sess.show_qr(content)
        log("等待扫码中（二维码约 3 分钟有效，过期会自动刷新）...")

        qr_deadline = time.time() + QR_TIMEOUT
        while time.time() < qr_deadline:
            time.sleep(POLL_INTERVAL)
            try:
                status, payload = sess.query(t, ck)
            except Exception as e:
                log(f"轮询失败，重试: {e}")
                continue
            if status != last_status:
                last_status = status
                tip = {"NEW": "等待扫码...",
                       "SCANED": "已扫码，请在手机上点「确认登录」",
                       "CONFIRMED": "已确认，正在落地登录态...",
                       "EXPIRED": "二维码已过期，刷新中...",
                       "CANCELED": "已取消，刷新中..."}.get(status, status)
                log(tip)
            if status in ("EXPIRED", "CANCELED"):
                break
            if status == "CONFIRMED":
                sess.finish_login(lg_token)
                if not sess.has_login():
                    # 兜底：可能响应里直接带了跳转地址
                    d = (payload.get("content") or {}).get("data") or {}
                    for key in ("url", "redirectUrl", "loginUrl", "targetUrl"):
                        if d.get(key):
                            try:
                                sess.fetch(str(d[key]), timeout=25)
                            except Exception:
                                pass
                    sess.finish_login(lg_token)
                n = sess.save()
                log(f"登录态已保存 {n} 条 cookie -> {STATE_FILE}")
                if not sess.has_login():
                    log("[WARN] 未发现登录标记 cookie（unb/cookie2/sgcookie），"
                        "登录可能未成功")
                print()
                log("下一步：执行 run.cmd check 验证，然后 run.cmd 启动监控")
                return 0
        else:
            log("本轮二维码超时，重新获取...")

    log("登录超时，请重试")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已取消")
        sys.exit(1)
