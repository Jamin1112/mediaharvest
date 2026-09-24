# mediaharvest 🕸️

一个把**网址丢进去就能抓图片和视频**的爬虫工具。命令行和本地网页界面都能用。

不用手动判断页面类型——它会自动在三种通道之间选择最合适的一个：

| 目标类型 | 处理通道 | 能搞定什么 |
|---|---|---|
| 普通静态网页 | HTTP + HTML 解析 | `img` / `video` / `srcset` / 懒加载 / CSS 背景图 / `og:` 元信息 / 内联脚本里的地址 |
| JS 动态网页 | 无头浏览器渲染 + 网络嗅探 | 小红书、微博、Instagram 这类内容靠 JS 加载的站点；嗅探 XHR 里的真实媒体地址 |
| 视频平台 | yt-dlp 站点适配 | 抖音、B站、YouTube、TikTok 等 1800+ 站点 |

流媒体方面内置 **HLS(m3u8)** 支持：自动选最高码流、AES-128 解密、分片并发下载、
拼接输出；**没有 ffmpeg 也能转成 MP4**（内置纯 Python 转封装实现）。

---

## 快速开始

```bash
./setup.sh          # 安装依赖（含无头浏览器）
./mh --selfcheck    # 确认环境就绪
```

### 命令行

```bash
./mh https://example.com/gallery               # 抓取并下载图片和视频
./mh https://example.com --list                # 只看有哪些资源，不下载
./mh https://example.com -t video              # 只要视频
./mh https://example.com -o ~/Downloads/media  # 指定保存目录
./mh "https://example.com/video.m3u8"          # 直接下载视频流
./mh https://example.com --interactive         # 交互式挑选要下载的
```

### 网页界面

```bash
./mh-web            # 自动打开 http://127.0.0.1:8848
```

界面里可以粘贴网址、看到缩略图预览、按类型筛选、勾选想要的资源再下载。
「保存目录」右侧的 **存为默认** 会把当前目录写进配置文件，下次打开就是它。

---

## 配置下载地址

不想每次都敲 `-o`，就把下载地址存进配置文件，CLI 和 Web 界面共用：

```bash
./mh --set-out-dir ~/Downloads/media    # 存为默认下载地址
./mh --show-config                      # 看当前生效的配置
./mh --unset-out-dir                    # 清除，回到 downloads
```

配置写在一个 TOML 文件里，可以直接编辑：

```toml
[download]
out_dir = "~/Downloads/media"
```

**优先级**（从高到低）：

1. 命令行 `-o ~/Downloads/other`
2. 环境变量 `MEDIAHARVEST_OUT_DIR`
3. 配置文件里的 `[download] out_dir`
4. 默认值 `downloads`

**配置文件位置**（取第一个存在的）：

1. `--config PATH` 指定的路径
2. 环境变量 `MEDIAHARVEST_CONFIG`
3. 当前目录的 `mediaharvest.toml`（项目级）
4. `~/.config/mediaharvest/config.toml`（用户级）

`mh --set-out-dir` 会写入候选列表里的第一个位置（默认项目级 `mediaharvest.toml`），
目录不存在会自动创建。路径支持 `~` 和 `$VAR`；相对路径按运行时的工作目录解析。
文件里其它键不会被覆盖，配置文件损坏时只会告警并回退到默认目录，不会中断抓取。

---

## 常用场景

**动态页面 / 内容加载不出来**

```bash
./mh https://example.com --render always --wait 3
```

**整站爬取**

```bash
./mh https://example.com --depth 2 --max-pages 30 --link-pattern '/post/\d+'
```

**需要登录的站点**（从已登录的浏览器导入 Cookie）

```bash
./mh https://example.com --cookies-from-browser chrome
```

**走代理 + 限制体积**

```bash
./mh https://example.com --proxy http://127.0.0.1:7890 --max-size 100M
```

**调试浏览器行为**（看窗口，便于排查）

```bash
./mh https://example.com --render always --show-browser
```

**只要某个分辨率以上的图**

```bash
./mh https://example.com --types image --json-file result.json
```

---

## 参数速查

| 参数 | 说明 |
|---|---|
| `-o, --output` | 保存目录（临时覆盖配置，默认取配置文件 / `downloads`） |
| `--set-out-dir` | 把下载地址写入配置文件 |
| `--unset-out-dir` | 清除配置里的下载地址 |
| `--show-config` | 显示当前生效的配置与候选文件 |
| `--config` | 指定配置文件路径 |
| `-t, --types` | 抓取类型：`image,video,audio,hls,dash` |
| `-c, --concurrency` | 下载并发数（默认 8） |
| `--render` | `auto` / `always` / `never`，浏览器渲染策略 |
| `--show-browser` | 显示浏览器窗口（调试） |
| `--ytdlp` | `auto` / `always` / `never`，站点适配策略 |
| `--depth` / `--max-pages` | 站内爬取深度与页面上限 |
| `--link-pattern` | 只跟随匹配该正则的链接 |
| `--same-host` | 只保留同域名资源 |
| `--segments` | 把 `.ts`/`.m4s` 分片也列为条目 |
| `--cookie` / `--cookies-from-browser` | 登录态 |
| `--proxy` | 代理地址 |
| `--max-size` / `--min-size` | 单文件体积上下限 |
| `--keep-ts` | 流媒体转成 MP4 后保留原始 `.ts` |
| `--hls-concurrency` | HLS 分片下载并发数 |
| `--interactive` | 下载前交互式挑选 |
| `--json-file` | 把结果导出为 JSON |
| `--list` | 只列出，不下载 |
| `--selfcheck` | 环境自检 |

完整参数：`./mh --help`

---

## 作为 Python 库使用

```python
import asyncio
from mediaharvest import Crawler, CrawlOptions, Downloader

async def main():
    options = CrawlOptions(render="auto", types=("image", "video"))
    async with Crawler(options) as crawler:
        report = await crawler.crawl("https://example.com")
        print(f"发现 {len(report.items)} 个资源")

        downloader = Downloader(crawler.fetcher, out_dir="downloads")
        results = await downloader.download_many(report.items)
        print(f"成功 {sum(1 for r in results if r.ok)} 个")

asyncio.run(main())
```

---

## 项目结构

```
mediaharvest/
├── models.py      数据模型（MediaItem / MediaType / Source）
├── utils.py       URL 清洗、类型判定、文件名生成
├── config.py      配置持久化（下载地址等，TOML）
├── fetcher.py     HTTP 层（重试、代理、Cookie、编码嗅探）
├── extractor.py   HTML 解析（8 类媒体发现渠道）
├── browser.py     无头浏览器渲染 + 网络嗅探
├── hls.py         m3u8 解析、AES-128 解密、分片下载拼接
├── remux.py       纯 Python MPEG-TS → MP4 转封装（无需 ffmpeg）
├── ytdlp.py       yt-dlp 集成（视频平台适配）
├── downloader.py  下载器（并发、重试、去重、命名）
├── crawler.py     编排层（三级策略自动升级）
├── cli.py         命令行入口
└── web.py         Web 界面（Flask）
```

---

## 工作原理

抓取按**成本递增**的顺序逐级尝试，够用就停：

1. **静态解析**——一次 HTTP 请求 + HTML 解析，最快最省资源。
   覆盖 `img`/`video`/`source`、懒加载属性（`data-src` 等）、`srcset` 多分辨率、
   CSS 背景图、`og:image`、内联脚本与 JSON-LD 里的地址、指向媒体的超链接。
2. **无头渲染**——结果太少、页面是 SPA 空壳、或属于已知动态站点时自动启用。
   渲染后重新解析 DOM（能拿到 JS 插入的元素），并**嗅探所有网络请求**，
   按 `Content-Type` 与 URL 特征挑出真实媒体地址。
3. **yt-dlp**——已知视频平台，或前两级都没找到视频时调用。

下载阶段：图片/直链视频走高并发 HTTP（带指数退避重试，429/5xx 自动重试）；
m3u8 走专用通道（选码 → 解密 → 并发拉分片 → 顺序拼接 → 转封装成 MP4）。

---

## 已知限制

- **DRM 加密流**（Widevine 等）无法下载，这是设计如此。
- **SAMPLE-AES** 加密的 HLS 需要 ffmpeg 或专用工具，内置实现不支持。
- **DASH (.mpd)** 需要 ffmpeg；没有会明确报错而不是产出坏文件。
- **纯 Python 转封装**支持 H.264 + AAC；HEVC/H.265 会跳过转封装并保留 `.ts`。
- 直播流（无 `#EXT-X-ENDLIST`）会被分片上限拦住，避免无限下载。
- 部分站点有强反爬（验证码、签名校验），需要手动提供 Cookie 或改用其它工具。
- Web 界面的图片预览走本地代理以绕过防盗链，该代理拒绝访问内网地址（防 SSRF）。

---

## 许可

MIT
