# xianyu-rss —— 闲鱼上新监控，直接输出 RSS

单文件、零第三方依赖（只用 Python 标准库），给 FreshRSS / 任何 RSS 阅读器供源。

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

实现是**纯 HTTP，不启浏览器**（接口来自闲鱼登录页自身）：

| 步骤 | 接口 |
|---|---|
| 取二维码 | `GET passport.goofish.com/newlogin/qrcode/generate.do?appName=xianyu&fromSite=77` → `codeContent`（二维码内容）、`t`、`ck` |
| 轮询状态 | `POST passport.goofish.com/newlogin/qrcode/query.do`，body `appName=xianyu&fromSite=77&t=&ck=` → `NEW` / `SCANED` / `CONFIRMED` / `EXPIRED` |
| 落地 cookie | 确认后访问 `qrcodeCheck.htm?lgToken=...`，再回访 `www.goofish.com` |

只有渲染二维码需要 `qrcode`（存 PNG 还需 `pillow`），监控运行时不需要它们。
如果没装，脚本会退化成直接打印二维码内容，你自己拿去生成也行：

```bat
D:\Program Files\python\python.exe -m pip install qrcode pillow
```

登录态过期时（表现为 `check` 失败、或大量 `FAIL_SYS_ILLEGAL_ACCESS`），重新跑一次 `run.cmd login`。

## 文件说明

| 文件 | 作用 |
|---|---|
| `xianyu_rss.py` | 抓取 + RSS 主逻辑，单文件、零依赖 |
| `xianyu_login.py` | 扫码登录，纯 HTTP；仅渲染二维码时需要 `qrcode` |
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
