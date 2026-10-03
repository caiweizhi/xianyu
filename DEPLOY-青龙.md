# 部署到青龙面板（定时抓 + 输出 RSS）

**结论：可以，而且很合适。** 这个项目是纯标准库，青龙里**不用装任何 pip 包**，
唯一要注意的是青龙的 long-running 方式、文件持久化路径和 RSS 出口。

## 一句话分工

> **青龙只负责定时抓**（`once` 模式，跑完就退出）
> **RSS 的 HTTP 出口另外找一个小容器**（nginx / python http.server）

青龙是 cron 调度器，不是进程守护器，**不要**在里面跑 `all` / `loop` / `serve`——
那些是常驻模式，青龙管不了，会残留进程。只用 `once`。

## 0. 先确认青龙里有 python3

进青龙容器执行：

```bash
docker exec -it qinglong python3 -V
```

官方 `whyour/qinglong` 镜像自带 python3。如果没有，在青龙「依赖管理 → Python」里
加 `qrcode` 时会自动触发 python 环境安装，或者：

```bash
docker exec -it qinglong bash -c "which apk && apk add python3 || apt-get install -y python3"
```

本项目抓取本身不需要任何 pip 包，只有 `login` 渲染二维码才需要 `qrcode`
（青龙里走「依赖管理 → Python」安装即可）。

## 1. 确认持久化路径（别把脚本放在会丢的地方）

> **新版青龙的脚本目录是 `/ql/data/scripts`，不是 `/ql/scripts`。**
> 只有老版本才是 `/ql/scripts`。放错地方，容器一重建脚本就没了。

```bash
docker inspect qinglong --format '{{range .Mounts}}{{.Source}} -> {{.Destination}}{{"\n"}}{{end}}'
```

找到挂载到 `/ql/data` 的那一行，左边的宿主机目录记作 `$QL_DATA`。把整个项目放到
`$QL_DATA/scripts/xianyu-rss/`：

```
$QL_DATA/scripts/xianyu-rss/
├── xianyu_rss.py
├── config.json
├── storage-state.json     ← 登录态
├── feeds/                 ← 生成的 RSS（后面会挂给 nginx）
└── （state.json、.lock 运行时自动生成）
```

不确定是新版还是老版？进容器看一眼：

```bash
docker exec qinglong ls -d /ql/data/scripts 2>/dev/null && echo 新版 || echo 老版/ql/scripts
```

## 2. 搞到登录态（二选一）

**方案 A（推荐）：在本机生成后传进去**

在你现在的 Windows 上跑 `run.cmd login` 扫码，然后把文件传进容器：

```bash
docker cp E:/05download/cs/xianyu-rss/storage-state.json qinglong:/ql/data/scripts/xianyu-rss/storage-state.json
```

（老版青龙把路径里的 `/ql/data/scripts` 换成 `/ql/scripts`）

**方案 B：直接在青龙任务里扫**

新建任务跑一次：

```
python3 /ql/data/scripts/xianyu-rss/xianyu_login.py
```

终端二维码会以 ASCII 形式打印在**任务日志**里（青龙日志是等宽字体，可以直接拿手机扫），
PNG 同时存在 `$QL_DATA/scripts/xianyu-rss/login-qr.png`，下载下来扫也行。

> ⚠️ **方案 B 需要浏览器，容器里得先装一次。**
> `xianyu_login.py` 现在默认用 headless Chromium 登录（跟 `xianyu-monitor-master`
> 的 `webbind.py` 同款），因为 `unb` / `sgcookie` / `tracknick` / `csg` / `tfstk` /
> `xlly_s` / `KLNotice` 这批身份 cookie **是页面 JS 写的，不是 `Set-Cookie` 下发的**——
> 只发请求拿 HTML 不会执行脚本，永远只能拿到 `cookie2`/`t`/`_tb_token_` 那 7 条，
> 抓搜索必挂 `RGV587_ERROR`。
>
> 装依赖（每个容器一次）：
>
> ```bash
> docker exec qinglong bash -c "pip install playwright qrcode pillow && playwright install chromium && playwright install-deps chromium"
> ```
>
> 装好后再跑上面的 `xianyu_login.py`；终端二维码会打印在任务日志里（等宽字体可直接扫），
> 同时存 `login-qr.png`。
> 如果容器里没装（或装失败），脚本会提示并问你要不要退回纯 HTTP 模式（残缺登录态），
> 选 N 就行，改走方案 A。
>
> **即使装了浏览器，也优先走方案 A**：容器长期挂着 headless Chromium 不如直接拷一份
> 已经验证过的 `storage-state.json` 稳。

## 2.5 抓不到数据？先跑诊断

```bash
docker exec -it qinglong python3 /ql/data/scripts/xianyu-rss/diag.py
```

它会一次性查出：登录态缺哪些关键 cookie、有没有被代理劫持、首页通不通、
mtop 握手返回什么、token 有没有拿到，最后给一张「可能原因 → 解决办法」表。
把完整输出贴出来就能定位。

**最常见的一条：`载入登录 cookie 只有 5 条`**

症状是这样的日志：

```
载入登录 cookie 5 条
[macmini] 第1页抓取失败: 接口返回异常: ['RGV587_ERROR::SM::哎哟喂,被挤爆啦,请稍后重试!']
```

这与抓取频率**无关**，是登录态残缺。本机实测对照：

| 登录态 | cookie 数 | 结果 |
|---|---|---|
| 完整（浏览器登录） | 19 | `SUCCESS::调用成功`，20 条 |
| 残缺（未走收尾链的纯 HTTP 登录） | 5~7 | `RGV587_ERROR`（**首次请求就失败，连 token 都不下发**） |

还有一种是**假 RGV587**：账号刚被高频请求触发风控时，哪怕 cookie 完全正常也会
连续几分钟报错，等 2~3 分钟自己恢复（实测 19 条完整登录态也复现过）。
区分办法——隔几分钟重跑 `diag.py`，能通就只是限流。

（判据是「身份凭证在不在」而不是「这一次搜索过没过」——账号处于风控窗口时
任何登录态都会报 RGV587，别把限流当成缺 cookie。）

靠谱的解法仍是从一台能正常抓取的机器上把登录态复制进容器：

```bash
docker cp E:/05download/cs/xianyu-rss/storage-state.json \
    qinglong:/ql/data/scripts/xianyu-rss/storage-state.json
docker exec -it qinglong python3 /ql/data/scripts/xianyu-rss/diag.py   # 再看一次
```

## 3. 建定时任务

青龙「定时任务 → 新建任务」：

| 项 | 值 |
|---|---|
| 名称 | 闲鱼上新监控 |
| 命令 | `python3 /ql/data/scripts/xianyu-rss/xianyu_rss.py once` |
| 定时规则 | `*/10 * * * *` |

> **间隔别低于 5 分钟。** 脚本自带文件锁：上一轮没跑完时新一轮会直接跳过
> （日志里会写「上一轮仍在执行，本轮跳过」），所以 cron 间隔比执行时间短也不会叠加请求，
> 但没必要压太狠——闲鱼风控看的是请求频次，10 分钟一轮 + 每轮 1~2 页足够。

先在青龙里手动「运行」一次，看日志有没有类似：

```
[12:00:01] 载入登录 cookie 19 条
[12:00:04]   [macmini] 抓到 30 条，新增 3 条
[12:00:07]   [机械键盘] 抓到 30 条，无新增
[12:00:07] 本轮新增 3 条，feed 已写入 /ql/data/scripts/xianyu-rss/feeds
```

看到这个就说明跑通了。

## 4. 让 FreshRSS 订阅到

青龙不提供 HTTP 服务，`feeds/` 需要一个出口。最简单是起一个 nginx 容器只读挂载它：

```yaml
# docker-compose.yml（和 FreshRSS 同一个 compose / 同一个 docker network 更好）
services:
  xianyu-feed:
    image: nginx:alpine
    container_name: xianyu-feed
    restart: unless-stopped
    volumes:
      - /你的宿主机路径/scripts/xianyu-rss/feeds:/usr/share/nginx/html:ro
    ports:
      - "8899:80"
```

然后 FreshRSS 订阅（文件名就是 config.json 里配的 `name`，没有百分号编码）：

- `http://<宿主机内网IP>:8899/macmini.xml`
- `http://<宿主机内网IP>:8899/keyboard.xml`

如果 FreshRSS 和这个 nginx 在**同一个 docker 网络**里，直接写 `http://xianyu-feed/macmini.xml`
连端口都不用暴露，更干净。

> 浏览器访问 `http://<IP>:8899/` 能看到 feed 文件清单（nginx 需要 `autoindex on`）。
> 文件名规则见 README「订阅地址」一节：中文关键词请在任务里配 `"name": "xxx"`。

## 5. 可选：接入青龙通知

在青龙「配置文件」里配好推送方式（Bark / 企业微信 / Telegram 等），然后把
`config.json` 的 `"notify": true`。之后**单轮单关键词新增 ≥10 条**时会自动推送一条汇总。

脚本会依次尝试 `/ql/shell/preload`、`/ql/shell` 两个目录去找青龙的 `notify.py`
（**新版青龙的真实路径是 `/ql/shell/preload/notify.py`**，只写 `/ql/shell` 有可能找不到）。
真找不到时静默跳过，不影响抓取。

## 6. 容器环境已经处理掉的坑

| 坑 | 表现 | 处理 |
|---|---|---|
| **LANG=C 导致中文崩** | `UnicodeEncodeError: 'ascii' codec...`，任务一行都跑不了 | 代码已强制 stdout/stderr 走 UTF-8，无需设 `PYTHONIOENCODING` |
| **cron 并发叠加** | 上一轮没结束下一轮又起，请求量翻倍 | 已加文件锁；僵死超过 15 分钟的锁自动清除 |
| **`all`/`loop` 残留进程** | 青龙里跑常驻模式会留一堆孤儿进程 | 青龙里只用 `once` |
| **`xdg-open` 不存在** | 容器内没有看图软件 | 已 try/except 兜底，不影响 ASCII 二维码和 PNG 保存 |
| **孤儿锁**（新增） | 上一次任务被强杀，锁没删干净，之后 15 分钟每轮都「跳过」 | 已加 PID 存活判断：属主进程死了立即接管锁 |
| **PID 复用**（新增） | 容器里 PID 很小容易撞号 | Linux 下改读 `/proc/<pid>/cmdline` 判断存活，比 `os.kill` 可靠 |
| **全局代理**（新增） | 青龙为拉 GitHub 脚本常配代理，urllib 会把闲鱼请求也塞进去，直接连不上 | 启动检测到代理会打印提示；加 `NO_PROXY=goofish.com,.goofish.com` 解决 |

关于代理这条：**实测过**。带上代理跑，结果是
`<urlopen error [WinError 10061] 由于目标计算机积极拒绝，无法连接。>`，一条都抓不到；
去掉代理（或加 `NO_PROXY`）立刻恢复正常。所以青龙里如果配了代理却在奇怪地失败，先看这一项。

## 7. config.json 在青龙里的调整建议

```jsonc
{
  "interval_seconds": 300,     // 对 once 模式无效（由 cron 控制），保留即可
  "notify": true,              // 想用青龙推送就开
  "feed_size": 100,
  "tasks": [
    { "keyword": "macmini", "max_price": 3000, "exclude": ["配件机"] }
  ]
}
```

改完不用重启任何东西，下一轮 cron 自动生效。

## 8. 上线前自检清单（按顺序对着点一遍）

```bash
# 1) 确认是新版路径还是老版路径
docker exec qinglong ls -d /ql/data/scripts && echo 新版 || echo 老版

# 2) python3 存在
docker exec qinglong python3 -V

# 3) 文件是否都到位了
docker exec qinglong ls -l /ql/data/scripts/xianyu-rss/

# 4) 亲自跑一次（最有价值的一步，日志会直接告诉你哪不对）
docker exec qinglong python3 /ql/data/scripts/xianyu-rss/xianyu_rss.py once

# 5) feed 有没有生成
docker exec qinglong cat /ql/data/scripts/xianyu-rss/feeds/macmini.xml | head -5
```

正常的话第 4 步会看到：

```
[12:00:01] 载入登录 cookie 19 条
[12:00:04]   [macmini] 抓到 30 条，新增 3 条
[12:00:07] 本轮新增 3 条，feed 已写入 ...
```

**异常速查**

| 日志 | 原因 | 处理 |
|---|---|---|
| `已生成默认配置: .../config.json` | `config.json` 没传进容器，跑的是内置默认（只有 1 个 macmini 任务） | 把本机 `config.json` `docker cp` 进去 |
| `[注意] 检测到代理 ...` + `urlopen error` | 青龙配了全局代理 | 加环境变量 `NO_PROXY=goofish.com,.goofish.com` |
| `载入登录 cookie 5 条` + `RGV587_ERROR` | **登录态残缺**（最常见），与频率无关 | 跑 `diag.py` 确认，再从能正常抓的机器 `docker cp storage-state.json` |
| `上一轮仍在执行，本轮跳过` 反复出现 | 上次被强杀留下锁（通常 15 分钟内自愈） | 确认无其他进程后删掉 `.lock` |
| `登录态已失效，重新扫码` | cookie 过期 | 重跑 `xianyu_login.py` 或本机 `run.cmd login` 后 `docker cp` |
| 凭证齐全但仍 `RGV587_ERROR` | 出口 IP 风险分高（容器 NAT / 机房 IP） | 间隔提到 30 分钟以上；或给青龙加 `network_mode: host` 走宿主网络 |
| `UnicodeEncodeError` | —— | 不该出现，代码已强制 UTF-8；出现请反馈 |
