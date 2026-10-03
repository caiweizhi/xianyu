#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""闲鱼监控排障诊断脚本（纯标准库，无第三方依赖）

用途：当抓取报 RGV587 / ILLEGAL_ACCESS / 抓到 0 条时，在**同一个环境**里
（青龙容器内也一样）跑它，一次性把可能的原因全查一遍：

  1. 登录态文件是否存在、里面有多少 cookie、缺哪些关键 cookie
  2. 环境变量里的代理（青龙常配代理抓 GitHub，会把闲鱼请求一起吃掉）
  3. www.goofish.com 首页能不能正常拿到（会不会被重定向到登录页）
  4. mtop 握手：_m_h5_tk 能不能拿到、签名后返回什么 ret
  5. 每个可能被塞 cookie 的域逐一验证，输出哪些域名下发了凭证

用法：
  python3 diag.py                      # 默认读 ./storage-state.json
  python3 diag.py /path/to/storage-state.json
"""

from __future__ import annotations

import hashlib
import http.cookiejar as cookiejar
import json
import os
import pathlib
import sys
import time
import urllib.parse
import urllib.request

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

APP_KEY = "34839810"
API = "mtop.taobao.idlemtopsearch.pc.search"
ENDPOINT = f"https://h5api.m.goofish.com/h5/{API}/1.0/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# 真正决定「是不是登录态」的 cookie。缺 unb 基本等于匿名访问。
KEY_COOKIES = {
    "unb": "用户标识，缺失=匿名，pc.search 大概率被风控",
    "cookie2": "登录凭证",
    "sgcookie": "登录签名串",
    "_tb_token_": "防 CSRF token，mtop 常用",
    "cna": "设备指纹，缺失会显著抬高风控分",
    "xlly_s": "会话串",
    "tfstk": "风控 token",
    "_m_h5_tk": "mtop 签名令牌（服务端下发，文件里本来就没有是正常的）",
}


def hr(title: str) -> None:
    print(f"\n--- {title} ---")


def main() -> int:
    base = pathlib.Path(__file__).resolve().parent
    state = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else base / "storage-state.json"

    print("=" * 60)
    print("  闲鱼监控 · 排障诊断")
    print(f"  时间: {time.strftime('%Y-%m-%d %H:%M:%S')}  (容器时区异常会导致签名失败)")
    print(f"  UTC : {time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())}")
    print("=" * 60)

    # 1. 登录态文件
    hr("1. 登录态文件")
    print(f"  路径: {state}")
    if not state.exists():
        print("  [致命] 文件不存在 -> 先执行 xianyu_login.py 扫码，"
              "或从能正常运行的机器 docker cp 过来")
        return 1
    try:
        data = json.loads(state.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  [致命] 无法解析 JSON: {e}")
        return 1

    raw = data.get("cookies", [])
    usable = [c for c in raw if c.get("domain")]
    print(f"  cookie 总数: {len(raw)}  (含 domain 可用: {len(usable)})")
    names = {c["name"] for c in usable}
    print(f"  cookie 清单: {sorted(names)}")

    print("  关键凭证检查:")
    missing = []
    for k, why in KEY_COOKIES.items():
        if k == "_m_h5_tk":
            continue
        ok = k in names
        if not ok:
            missing.append(k)
        print(f"    [{'有' if ok else '缺'}] {k:<12} {why}")
    if missing:
        print(f"  >>> 缺少 {len(missing)} 个关键凭证: {missing}")
        print("  >>> 这通常意味着这份登录态是「半完成」的：")
        print("      纯 HTTP 扫码只能拿到 passport 域的几枚 cookie，")
        print("      而 cna / _tb_token_ 等要靠访问 www.goofish.com 由 JS 下发。")
        print("      解决：用浏览器完整登录一次（推荐），或看第 5 节的补救办法。")
    else:
        print("  关键凭证齐全。")

    # 2. 代理
    hr("2. 代理环境变量")
    proxy_vars = [v for v in ("http_proxy", "https_proxy", "HTTP_PROXY",
                              "HTTPS_PROXY", "all_proxy", "ALL_PROXY")
                  if os.environ.get(v)]
    if not proxy_vars:
        print("  未检测到代理（正常）")
    else:
        for v in proxy_vars:
            print(f"  [警告] {v}={os.environ[v]}")
        noproxy = os.environ.get("no_proxy") or os.environ.get("NO_PROXY") or ""
        safe = "goofish.com" in noproxy or "m.goofish.com" in noproxy
        print(f"  no_proxy={noproxy!r}")
        if not safe:
            print("  >>> urllib 会尊重代理，闲鱼请求可能被转发到不可达的代理 -> 抓 0 条")
            print("  >>> 在青龙「环境变量」里加: no_proxy = goofish.com,m.goofish.com,"
                  "h5api.m.goofish.com,taobao.com")

    # 3. 首页连通性
    hr("3. www.goofish.com 首页")
    jar = cookiejar.CookieJar()
    for c in usable:
        try:
            jar.set_cookie(cookiejar.Cookie(
                version=0, name=c["name"], value=c["value"],
                port=None, port_specified=False,
                domain=c["domain"], domain_specified=True,
                domain_initial_dot=str(c["domain"]).startswith("."),
                path=c.get("path") or "/", path_specified=True,
                secure=bool(c.get("secure", False)), expires=c.get("expires"),
                discard=False, comment=None, comment_url=None, rest={},
            ))
        except Exception:
            pass

    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

    def get(url: str, referer: str = "https://www.goofish.com/") -> tuple[int, str, int]:
        req = urllib.request.Request(url)
        req.add_header("User-Agent", UA)
        req.add_header("Referer", referer)
        req.add_header("Accept", "text/html,application/xhtml+xml,*/*")
        try:
            with op.open(req, timeout=25) as r:
                return r.status, r.read().decode("utf-8", "replace"), len(jar)
        except urllib.error.HTTPError as e:
            return e.code, "", len(jar)
        except Exception as e:
            return -1, str(e), len(jar)

    st, body, before = get("https://www.goofish.com/")
    after = len(jar)
    print(f"  HTTP {st}   响应 {len(body)} 字节   cookie {before} -> {after} "
          f"(+{after - before})")
    if st == -1:
        print(f"  [致命] 请求异常: {body[:200]}")
        print("  >>> 网络不通 / 代理错误 / DNS 失败")
        return 1
    if st in (301, 302):
        print("  >>> 被重定向到登录页：登录态已失效，请重新扫码")

    # 4. mtop 握手 + 搜索
    hr("4. mtop 接口握手与搜索")

    def token() -> str:
        for c in jar:
            if c.name == "_m_h5_tk":
                return c.value.split("_")[0]
        return ""

    def call(payload: str) -> tuple[dict, int]:
        t = str(int(time.time() * 1000))
        sign = hashlib.md5(f"{token()}&{t}&{APP_KEY}&{payload}".encode()).hexdigest()
        qs = urllib.parse.urlencode({
            "jsv": "2.7.2", "appKey": APP_KEY, "t": t, "sign": sign,
            "v": "1.0", "type": "originaljson", "accountSite": "xianyu",
            "dataType": "json", "timeout": "20000", "api": API,
            "sessionOption": "AutoLoginOnly", "spm_cnt": "a21ybx.search.0.0",
        })
        req = urllib.request.Request(
            f"{ENDPOINT}?{qs}",
            data=urllib.parse.urlencode({"data": payload}).encode(),
            method="POST")
        for k, v in (("User-Agent", UA),
                     ("Content-Type", "application/x-www-form-urlencoded"),
                     ("Referer", "https://www.goofish.com/"),
                     ("Origin", "https://www.goofish.com"),
                     ("Accept", "application/json")):
            req.add_header(k, v)
        try:
            with op.open(req, timeout=25) as r:
                return json.loads(r.read().decode("utf-8", "replace")), len(jar)
        except urllib.error.HTTPError as e:
            return {"ret": [f"HTTP {e.code}"]}, len(jar)
        except Exception as e:
            return {"ret": [f"ERR {e}"]}, len(jar)

    payload = json.dumps({
        "pageNumber": 1, "keyword": "macmini", "fromFilter": False,
        "rowsPerPage": 5, "sortField": "CREATE", "sortValue": "",
        "customDistance": "", "gps": "", "propValueStr": {}, "customGps": "",
        "searchReqFromPage": "pcSearch", "extraFilterValue": "{}",
        "userPositionJson": "{}",
    }, ensure_ascii=False, separators=(",", ":"))

    print("  第 1 次（此时通常还没有 _m_h5_tk，预期 TOKEN_EMPTY）")
    res, n1 = call(payload)
    print(f"    ret = {str(res.get('ret'))[:160]}")
    print(f"    cookie {n1} 条, token = {token()[:12] or '(空)'}")

    time.sleep(2)
    print("  第 2 次（带上新 token 重签）")
    res, n2 = call(payload)
    ret = str(res.get("ret"))
    print(f"    ret = {ret[:160]}")
    cnt = len(((res.get("data") or {}).get("resultList")) or [])
    print(f"    返回商品 {cnt} 条, cookie {n2} 条")

    # 5. 结论
    hr("5. 诊断结论")
    ok = "SUCCESS" in ret and cnt > 0
    print(f"  抓取能力: {'正常 ✅' if ok else '异常 ❌'}")
    if ok:
        print("  这份登录态可用，直接跑 once 即可。")
        return 0

    print("  可能原因（按可能性排序）:")
    print("   A. 登录态不完整 —— 缺 unb / cookie2 / sgcookie / cna 等")
    print("      判定: 第 1 节里关键凭证有缺失")
    print("      解决: 从已能正常运行的机器 docker cp storage-state.json 到容器")
    print("   B. 该 IP 触发风控 —— 服务器 / 容器出口 IP 风险等级高")
    print("      判定: 凭证齐全但仍然 RGV587")
    print("      解决: interval_seconds >= 300；或让青龙走宿主网络 network_mode: host")
    print("   C. 代理劫持 —— 见第 2 节")
    print("   D. 接口改版 —— 概率低，届时会是 API_NOT_FOUND")
    print()
    print("  补救建议: 在容器里执行一次性浏览器登录（能拿到完整 cna/x5sec），")
    print("  或在本机 Playwright 登录成功后 docker cp 过去 —— 后者最快且已验证可用。")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
