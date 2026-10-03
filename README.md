# xianyu-rss —— 闲鱼上新监控，直接输出 RSS

单文件、运行时零第三方依赖（只用 Python 标准库），给 FreshRSS / 任何 RSS 阅读器供源。
唯一需要 pip 包的是**扫码登录**那一步（`playwright` + `qrcode`，详见[依赖](#依赖pip-装什么)）；
抓取和 RSS 本身一个包都不装。

## 原理

```
storage-state.json 里的 cookie
        │
        ▼
mtop 搜索接口（md5 签名，sign = MD5(token & t & "34839810" & data)）
        │  sortField=CREATE → 按最新发布降序
        ▼
按 itemId 去重（state.json）
        ▼
生成 RSS 2.0 → feeds/<关键词>.xml → HTTP 服务 :8899
```

`_m_h5_tk`（签名 token）不需要你手工准备：首次请求服务端会下发，`http.cookiejar` 自动收纳，
脚本拿到后重签一次即可。token 过期（`FAIL_SYS_ILLEGAL_ACCESS`）时同样自动重试一次。

## 用法

```bat
run.cmd                 :: 默认 all：后台定时抓取 + 前台提供 RSS
run.cmd once            :: 只抓一轮（适合挂 Windows 任务计划 / cron）
run.cmd check           :: 只验证登录态和接口是否还通
run.cmd serve           :: 只起 HTTP 服务，不抓取
```

不用 `run.cmd` 也可以：

```bat
D:\Program Files\python\python.exe xianyu_rss.py all
```

Linux / Docker（青龙）下用 `python3`，并且**只用 `once`**（由 cron 调度）：

```bash
python3 /ql/scripts/xianyu-rss/xianyu_rss.py once
python3 xianyu_login.py        # 扫码登录
```

`all` / `loop` / `serve` 是常驻模式，**不适合**放在青龙里跑。
想放到家里服务器的青龙面板上定时跑 → 看 [DEPLOY-青龙.md](DEPLOY-青龙.md)。

## 环境变量

| 变量 | 作用 |
|---|---|
| `XIANYU_RSS_DIR` | 把 config.json / state.json / feeds 放到别处（容器里想单独挂卷时用），不设就用脚本所在目录 |

## 订阅地址

服务起来后（默认端口 8899），**直接打开 <http://127.0.0.1:8899/>**，页面上会把每个关键词的
订阅地址列出来，点一下就能打开 feed。

命名规则：`name` > `keyword`（仅当 keyword 是纯英文数字）> 自动生成。
**中文关键词务必配一个 `name`**，例如 `"keyword": "机械键盘", "name": "keyboard"`，
这样地址就是干净的 `keyboard.xml`；不配的话会退化成 `x<哈希>.xml`。

- `http://<本机IP>:8899/macmini.xml`
- `http://<本机IP>:8899/keyboard.xml`

FreshRSS 里「订阅 → 添加 RSS 源」，一个关键词一条源。**不要用 127.0.0.1**，
如果 FreshRSS 跑在 Docker 里，填宿主机的局域网 IP（启动时日志会把它打印出来）。

## 配置（config.json）

```jsonc
{
  "cookie_state": "storage-state.json",   // 闲鱼登录态
  "interval_seconds": 300,                // 抓取间隔，别低于 180
  "feed_size": 100,                       // 每个 feed 最多保留多少条
  "request_delay": [1.5, 4.0],            // 请求间随机休眠区间（防风控）
  "serve_port": 8899,
  "tasks": [
    {
      "keyword": "机械键盘",                 // 真正拿去搜索的词
      "name": "keyboard",                 // feed 文件名/URL，中文关键词必配（英文短名）
      "min_price": null,                  // 最低价，null = 不限
      "max_price": 500,                   // 最高价
      "exclude": ["坏的", "尸体"],       // 标题含这些词就丢弃
      "include": [],                      // 标题必须含这些词（空 = 不限）
      "pages": 1,                         // 抓几页
      "rows_per_page": 30
    }
  ]
}
```

> `name` 省略时：keyword 是纯英文数字就直接用；都不满足则退化成 `x<哈希>.xml`。
> 所以**中文关键词请一定写上 `name`**，否则地址不好记。

改完 `config.json` 后重启即可生效。

## 登录 / 重新登录

**本项目自带扫码登录，不依赖任何其他项目**：

```bat
run.cmd login
```

终端会直接打印二维码（同时存一份 `login-qr.png` 并自动用看图软件打开），
用**闲鱼 App** 扫码并在手机上点「确认登录」即可，登录态写入 `storage-state.json`。
二维码约 3 分钟有效，过期会自动刷新；整体超时 7 分钟。

### 为什么必须启一个真浏览器

这一点踩过坑，先说结论。实测两种做法：

| 做法 | 拿到的 cookie | 抓搜索 |
|---|---|---|
| 纯 HTTP：登录页 + `generate.do` + `query.do` + 收尾跳转 | 7 条（`XSRF-TOKEN`/`_samesite_flag_`/`cookie2`/`t`/`_tb_token_`/`cna`/`sca`） | `RGV587_ERROR::SM::哎哟喂,被挤爆啦` |
| 浏览器（headless Chromium） | 19 条，含 `unb`/`sgcookie`/`tracknick`/`csg`/`tfstk`/`xlly_s`/`KLNotice` | `SUCCESS::调用成功` |

差的这 12 条**不是 `Set-Cookie` 下发的，是 `www.goofish.com` 页面里 JS 算出来写进去的**：
登录页自己的前端 `havana-nlogin/index.js` 里压根没出现过这几个名字，
纯 HTTP 拿到 HTML 也不会执行脚本。所以**纯 HTTP 有上限，别再试了**。

登录脚本因此默认启一个 headless Chromium，流程（与 `xianyu-monitor-master` 的
`webbind.py` 同款）：

| 步骤 | 动作 |
|---|---|
| 开浏览器 | `chromium.launch(headless=True)`，locale `zh-CN`、时区 `Asia/Shanghai` |
| 打开登录页 | `passport.goofish.com/mini_login.htm?...&qrCodeFirst=true&stie=77`，并**拦截**它自己发出的 `newlogin/qrcode/generate.do` |
| 出码 | 从 `content.data.codeContent` 取出内容 → 终端画半块字符二维码 + 存 `login-qr.png` 自动打开 |
| 等待 | 页面自己轮询 `query.do`，`page.on("response")` 捕获状态 `NEW`/`SCANED`/`CONFIRMED`/`EXPIRED` |
| **落地 cookie** | CONFIRMED 后持续 `context.cookies()` 轮询，等出现 `sgcookie`/`unb`/`tracknick`/`lgc`/`uc1`/… 再存盘 |
| 存盘 | `context.storage_state()` → `storage-state.json`（含 httpOnly cookie） |

### 参数

```bat
run.cmd login                    REM 浏览器扫码（默认，推荐）
run.cmd login --no-verify        REM 跳过真接口校验
run.cmd login --no-merge         REM 不要用旧登录态补缺失的 cookie
run.cmd login --headful          REM 有头窗口，方便看页面（调试用）
run.cmd login --http             REM 退回纯 HTTP 流程，大概率残缺，只是留个后路
```

落盘前会拿真接口验一遍。**判据是身份凭证在不在（`unb`/`sgcookie`/`tracknick`/…），
不是单次搜索结果**——账号刚被风控时任何登录态都会 RGV587，那种情况只提示不动文件。
如果扫完确实缺凭证，脚本**默认自动用旧登录态（`storage-state.bak.json`）补洞**再验。

### 依赖（pip 装什么）

清单在仓库根的 [`requirements.txt`](requirements.txt)，分三层，**绝大多数情况你什么都不用装**：

| 用到哪 | 包 | 是不是必须 | 缺了会怎样 |
|---|---|---|---|
| 抓取 + 生成 RSS + `serve` / `once` / `check` / `diag` | *无*（纯标准库） | ✅ 必须，**但不用装** | —— `xianyu_rss.py` / `diag.py` 只 import `urllib`、`http.cookiejar`、`hashlib`、`http.server`、`json`、`xml.sax.saxutils`、`email.utils` |
| 扫码登录（默认流程） | `playwright` | 必须 | 脚本提示「浏览器登录不可用」并询问是否退回纯 HTTP（纯 HTTP 只有 7 条 cookie，必挂 `RGV587`） |
| 扫码登录 | `qrcode` | 必须（否则看不到码） | 终端画不出二维码，脚本改为打印二维码原文，你自己拿去生成 |
| 扫码登录 | `pillow` | **可选** | 没有也能存出 `login-qr.png`——`qrcode` 自带纯 Python PNG 后端，会回退到 `qrcode.image.pure.PyPNGImage` |

> 一句话：**只有跑 `run.cmd login` 才需要装东西，日常跑 `run.cmd`（或 `run.cmd once`）零依赖。**

#### 一台干净机器上怎么装

```bat
:: 1) 只为了登录：playwright + qrcode（pillow 可省略）
D:\Program Files\python\python.exe -m pip install -r requirements.txt

:: 2) playwright 还要单独下浏览器内核（约 150MB，装一次即可）
D:\Program Files\python\python.exe -m playwright install chromium

:: 3) 自检：三个都 import 得到才算齐
D:\Program Files\python\python.exe -c "import playwright, qrcode; import PIL; print('ok')"
```

`playwright install chromium` 是**两步**：`pip install` 只装 Python 包，浏览器内核
（`%LOCALAPPDATA%\ms-playwright\chromium-*`）得单独下，漏了这步启动会报找不到浏览器。

#### 本机（Windows）现状

`run.cmd` 用的 `D:\Program Files\python`（**Python 3.12.14**）已经装齐，直接跑就行：

```
playwright 1.63.0      qrcode 8.2      pillow 12.3.0
ms-playwright\chromium-1243 + chromium_headless_shell-1243
```

想换别的解释器（比如别的 Python）再补装上面的三步即可。

#### Linux / 青龙容器

- 只跑 `python3 xianyu_rss.py once`：**零依赖**，容器里一个 pip 包都不用装。
- 想在容器内自己扫码登录：容器里得有 playwright + 浏览器内核，但 **Chromium 需要系统依赖库**，
  自行 apt 装 `chromium` 再 `pip install playwright` 并 `playwright install chromium` 更省心。
  更稳的做法仍是**本机登录好后把 `storage-state.json` 拷进容器**（见 [DEPLOY-青龙.md](DEPLOY-青龙.md)）。

登录态过期时（表现为 `check` 失败、或大量 `FAIL_SYS_ILLEGAL_ACCESS`），重新跑一次 `run.cmd login`。

## 抓不到数据怎么办

先跑诊断，它会直接告诉你原因：

```bat
run.cmd diag
REM Linux / 青龙里：python3 diag.py
```

输出会覆盖：登录态缺哪些关键 cookie、有没有被代理劫持、首页通不通、mtop 握手每一步的
返回码，最后给一张「可能原因 → 解决办法」表。

**最常见两个坑：**

1. **登录态残缺。** 缺 `unb` / `sgcookie` / `csg` / `tfstk` / `xlly_s` / `tracknick`
   等时，mtop 直接返回：

   ```
   FAIL_SYS_USER_VALIDATE, RGV587_ERROR::SM::哎哟喂,被挤爆啦,请稍后重试!
   ```

   实测逐条裁 cookie 的边缘：任何单条缺失还能跑，但低于完整 19 条就会挂——
   `sgcookie` + `tfstk` + `xlly_s` + `csg` + `unb` 这一组是风控判定的核心。
    解决：跑 `run.cmd login`（新流程会自己补齐这些），或把别处完整的
   `storage-state.json` 复制过来。

2. **账号处于风控窗口（假的 RGV587）。** 高频请求之后，哪怕 cookie 完全正常，
   也会连着几分钟报同一句 RGV587，等 2~3 分钟自己就恢复。
   判断方法：隔几分钟重跑 `run.cmd diag`；能通就说明只是限流，别急着重新登录。

## 文件说明

| 文件 | 作用 |
|---|---|
| `xianyu_rss.py` | 抓取 + RSS 主逻辑，单文件、零依赖 |
| `diag.py` | 排障诊断：抓不到数据时跑它，一次性查登录态/代理/握手/接口返回 |
| `xianyu_login.py` | 扫码登录（headless Chromium，可 `--http` 退回纯 HTTP）；需 `playwright`+`qrcode`（`pillow` 可选） |
| `requirements.txt` | 依赖清单（分层注释，运行时零依赖） |
| `config.json` | 关键词与过滤条件 |
| `state.json` | 已见商品 ID + feed 缓存（自动生成，删了会重新推送一遍） |
| `feeds/*.xml` | 生成的 RSS |
| `storage-state.json` | 闲鱼登录态（由 `run.cmd login` 生成） |
| `run.cmd` | Windows 启动器（便携 Python），关窗口即停 |
| `start-detached.cmd` | 后台常驻启动器：独立最小化窗口运行，关终端不影响，日志写 `logs/run.log` |
| `logs/run.log` | 常驻模式的运行日志 |
| `DEPLOY-青龙.md` | 部署到青龙面板的实操步骤 |
| `.lock` | 并发锁，运行时临时生成，正常退出会删除；属主进程已死时会被自动接管 |
| `.gitignore` | 避免 feeds / state / 登录态 / 日志被误提交 |

## 后台常驻怎么起

`run.cmd` 是**前台**的，窗口一关服务就停。要长期挂着用：

```bat
start-detached.cmd          :: 独立窗口运行，关闭当前终端不影响
taskkill /FI "WINDOWTITLE eq xianyu-rss*" /F      :: 停掉它
tail logs\run.log           :: 看日志
```

想开机自启的话，把 `start-detached.cmd` 丢进「任务计划程序」建一个登录触发器即可。
机器上装了 [NSSM](https://nssm.cc) 的话用 `nssm install xianyu-rss "<路径>\start-detached.cmd"`
还能拿到崩溃自动重启。

## 注意

- 抓取间隔别压太低。闲鱼对家宽 IP 比对机房 IP 宽容，但 300 秒一轮 + 每轮 1~2 页已经足够，
  真要更快就把 `interval_seconds` 调到 180，不要低于这个值。
- `pubDate` 用的是商品真实发布时间（`clickParam.args.publishTime`），不是入库时间。
- 首次运行会把当前第一页的全部结果当作「已存在」推送一次，之后只推新增。
