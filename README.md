# mediaharvest 🕸️

一个把**网址丢进去就能抓图片、视频和音乐**的爬虫工具。命令行和本地网页界面都能用。

不用手动判断页面类型——它会自动在四种通道之间选择最合适的一个：

| 目标类型 | 处理通道 | 能搞定什么 |
|---|---|---|
| 普通静态网页 | HTTP + HTML 解析 | `img` / `video` / `srcset` / 懒加载 / CSS 背景图 / `og:` 元信息 / 内联脚本里的地址 |
| JS 动态网页 | 无头浏览器渲染 + 网络嗅探 | 小红书、微博、Instagram 这类内容靠 JS 加载的站点；嗅探 XHR 里的真实媒体地址 |
| 视频平台 | yt-dlp 站点适配 | 抖音、B站、YouTube、TikTok 等 1800+ 站点 |
| 音乐平台 | 音质选择 + 专辑/歌单展开 + 标签写入 | 网易云、SoundCloud 等；整张专辑/歌单抓取，自动写 ID3 标签、歌词与封面 |

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

./mh "https://music.163.com/song?id=347230"                # 抓一首歌
./mh "https://music.163.com/playlist?id=3778678"           # 整张歌单
./mh "https://music.163.com/song?id=347230" --quality lossless  # 只要无损
./mh --music-info "https://y.qq.com/n/ryqq/songDetail/xxx" # 先看这个链接能不能抓
```

### 网页界面

```bash
./mh-web            # 自动打开 http://127.0.0.1:8848
```

界面里可以粘贴网址、看到缩略图预览、按类型筛选、勾选想要的资源再下载。
「保存目录」右侧的 **存为默认** 会把当前目录写进配置文件，下次打开就是它。
高级选项里有**音质**、**专辑/歌单是否整张抓**、**是否写音乐标签**三个开关。

界面默认深色主题，右上角按钮可切换浅色（选择会记在浏览器里）。几个顺手的细节：

- `/` 聚焦地址栏，`Esc` 清空当前勾选，`⌘/Ctrl + Enter` 直接分析或下载。
- 每个资源卡片右下角的按钮可在新标签页打开原始地址，不改变勾选状态。
- 分析过程中的进度、下载的实时速度与文件数都在页面上，完成后右下角有轻提示。

---

## 音乐抓取

丢一个音乐链接进去就行，工具会自动判断这是单曲、专辑、歌单还是歌手，
并选好音质：

```bash
./mh "https://music.163.com/song?id=347230"      # 单曲
./mh "https://music.163.com/playlist?id=3778678" # 歌单 → 每首歌一条
./mh "https://soundcloud.com/artist/sets/xxx"    # SoundCloud 合集
```

抓下来的是什么样：

```
download/2026-09-24_161500_海阔天空/
└── audio/
    └── Beyond - 海阔天空/
        ├── Beyond-海阔天空.mp3     ← 已写入 ID3：标题/歌手/专辑/年份/封面/歌词
        └── Beyond-海阔天空.lrc     ← 用 --lrc-file 时额外生成
```

### 音质档位

| 档位 | 说明 |
|---|---|
| `best` | **最高音质**（默认）：无损优先，没有无损就取码率最高的有损 |
| `lossless` | **仅无损**：只要 flac/alac/wav，**没有无损源就跳过这首**并明确告知 |
| `high` | 高音质：按编码偏好选（m4a 优先，同感知质量下更省空间） |
| `medium` | 中等音质：贴近 192kbps |
| `low` | 省流：贴近 128kbps |

`best` 与 `lossless` 的区别值得注意：前者「尽力给最好的」，后者「宁缺毋滥」——
想建无损库就用 `lossless`，它会诚实告诉你哪几首没有无损源，而不是悄悄给个 mp3。

### 标签、歌词与封面

下载完成后会自动写入音乐标签，**优先用 mutagen，没装则回退到内置的纯 Python 实现**
（手写 ID3v2.3 / FLAC Vorbis comment / MP4 `ilst` 原子），所以无额外依赖也能用：

```bash
./mh <音乐网址> --no-tags      # 不写标签，只存音频
./mh <音乐网址> --no-lyrics    # 不抓歌词
./mh <音乐网址> --no-cover     # 不抓封面
./mh <音乐网址> --lrc-file     # 额外存一份 .lrc 文件
```

歌词支持双语合并：网易云同时返回原文与翻译时，会按时间轴对齐合并成双语 LRC。

### 平台支持情况

**能抓的**（实测可用）：

| 平台 | 说明 |
|---|---|
| 网易云音乐 | 单曲、歌单、歌手；歌词与封面齐全 |
| SoundCloud / Mixcloud / Audiomack | 单曲与合集 |
| Bandcamp | 单曲与专辑 |
| Jamendo / archive.org | 单曲与专辑 |
| 蜻蜓FM | 音频节目 |
| YouTube Music | 按普通 YouTube 视频解析 |
| B站音频 | 仅 `/audio/auXXXX` 路径 |

**抓不了的**（会明确报错，不会假装成功）：

| 平台 | 原因 |
|---|---|
| Spotify / Apple Music / Tidal | DRM 保护 |
| QQ音乐 / 酷狗 / 酷我 / 咪咕 | 接口需签名校验或无可用解析后端 |

```bash
./mh --music-info "<网址>"    # 抓之前先确认平台与类型
```

> **关于专辑**：网易云的专辑接口依赖 yt-dlp 的提取器，而该提取器在
> yt-dlp 2025.10.14 上已失效（上游问题）。歌单与歌手可以正常整张抓取；
> 专辑目前会明确报错。等上游修复即可自动恢复，无需改动本项目。

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

[music]
quality = "lossless"      # 只要无损
expand_playlists = true   # 专辑/歌单整张抓
lyrics = true             # 抓歌词
cover = true              # 抓封面
tags = true               # 写入 ID3 标签
```

仓库里提供了一个安全的示例文件，可以复制后按自己的机器修改：

```bash
cp mediaharvest.example.toml mediaharvest.toml
./mh --show-config
```

真实的 `mediaharvest.toml` 已加入 `.gitignore`，不建议上传，因为里面可能包含本机路径、
代理、Cookie 或浏览器登录相关配置。

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
| `--quality` | 音质档位：`best`/`lossless`/`high`/`medium`/`low` |
| `--no-music` | 关闭音乐通道（音乐 URL 按普通网页处理） |
| `--no-playlist` | 不展开专辑/歌单/歌手，只抓单个目标 |
| `--playlist-limit` | 单个专辑/歌单最多抓多少首（默认 200） |
| `--no-lyrics` / `--no-cover` / `--no-tags` | 分别关闭歌词 / 封面 / 标签写入 |
| `--lrc-file` | 在音频文件旁额外保存 `.lrc` 歌词文件 |
| `--music-info` | 只识别音乐 URL 的平台与类型后退出 |
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

抓音乐：专辑/歌单会整张展开，每条曲目都带 :class:`MusicMeta`（歌手、专辑、
曲目号、歌词、封面），下载后自动写入标签。

```python
import asyncio
from mediaharvest import Crawler, CrawlOptions, Downloader, classify_music_url

async def main():
    url = "https://music.163.com/playlist?id=3778678"
    print(classify_music_url(url).kind.label)      # 歌单

    options = CrawlOptions(types=("audio",), quality="lossless",
                           expand_playlists=True, playlist_limit=50)
    async with Crawler(options) as crawler:
        report = await crawler.crawl(url)
        for item in report.items:
            if item.music:
                print(item.music.display, "|", item.music.album,
                      "| 歌词", len(item.music.lyrics), "字")

        downloader = Downloader(crawler.fetcher, out_dir="downloads",
                                music_tags=True)
        await downloader.download_many(report.items)

asyncio.run(main())
```

---

## 项目结构

```
mediaharvest/
├── models.py      数据模型（MediaItem / MediaType / Source / MusicMeta / TrackKind）
├── utils.py       URL 清洗、类型判定、文件名生成
├── config.py      配置持久化（下载地址等，TOML）
├── fetcher.py     HTTP 层（重试、代理、Cookie、编码嗅探）
├── extractor.py   HTML 解析（8 类媒体发现渠道）
├── browser.py     无头浏览器渲染 + 网络嗅探
├── hls.py         m3u8 解析、AES-128 解密、分片下载拼接
├── remux.py       纯 Python MPEG-TS → MP4 转封装（无需 ffmpeg）
├── ytdlp.py       yt-dlp 集成（视频与音乐平台适配）
├── music.py       音乐支持（平台识别、目标分类、音质选择、曲目元信息）
├── lyrics.py      歌词抓取（按平台接口取 LRC 并与译文合并）
├── tags.py        音乐标签写入（mutagen，缺失时回退纯 Python 实现）
├── downloader.py  下载器（并发、重试、去重、命名、音乐后处理）
├── crawler.py     编排层（四级策略自动升级）
├── cli.py         命令行入口
└── web.py         Web 界面（Flask）
```

---

## 工作原理

抓取按**成本递增**的顺序逐级尝试，够用就停：

1. **音乐通道**——URL 命中已知音乐平台时优先走这里。
   先判断目标是单曲还是专辑/歌单/歌手，集合目标交给 yt-dlp 展开成多条曲目，
   再按音质档位从各条音频流里选（无损优先、编码偏好、目标码率），
   最后补齐歌词与封面。命中音乐平台但解析失败时**不再回退**到通用通道——
   那只会捞回几十张封面图，把真正的原因淹没掉。
2. **静态解析**——一次 HTTP 请求 + HTML 解析，最快最省资源。
   覆盖 `img`/`video`/`source`、懒加载属性（`data-src` 等）、`srcset` 多分辨率、
   CSS 背景图、`og:image`、内联脚本与 JSON-LD 里的地址、指向媒体的超链接。
3. **无头渲染**——结果太少、页面是 SPA 空壳、或属于已知动态站点时自动启用。
   渲染后重新解析 DOM（能拿到 JS 插入的元素），并**嗅探所有网络请求**，
   按 `Content-Type` 与 URL 特征挑出真实媒体地址。
4. **yt-dlp**——已知视频平台，或前几级都没找到视频时调用。

下载阶段：图片/直链视频走高并发 HTTP（带指数退避重试，429/5xx 自动重试）；
m3u8 走专用通道（选码 → 解密 → 并发拉分片 → 顺序拼接 → 转封装成 MP4）；
音乐文件下载完成后额外写标签、歌词与封面（写失败不影响音频本身）。

---

## 已知限制

- **DRM 加密流**（Widevine 等）无法下载，这是设计如此；Spotify/Apple Music/Tidal 因此不可用。
- **QQ音乐 / 酷狗 / 酷我 / 咪咕**需要签名校验或无可用解析后端，当前抓不了（会明确报错）。
- **网易云专辑**依赖的 yt-dlp 提取器在 2025.10.14 上已失效（上游问题），
  歌单与歌手正常；上游修复后自动恢复。
- **SAMPLE-AES** 加密的 HLS 需要 ffmpeg 或专用工具，内置实现不支持。
- **DASH (.mpd)** 需要 ffmpeg；没有会明确报错而不是产出坏文件。
- **纯 Python 转封装**支持 H.264 + AAC；HEVC/H.265 会跳过转封装并保留 `.ts`。
- **OGG/Opus 标签**在纯 Python 回退模式下不支持（OGG 页级重写过于复杂），
  会明确报错；装了 mutagen 即可正常写入。支持 `.ogg`/`.oga`/`.ogx`/`.opus`。
- **WebM/Matroska（`.weba`）** 标签不支持：mutagen 1.47 无对应解析器，
  强行按 Ogg 处理会写坏文件，因此明确报「不支持」。
- **分片 MP4（fMP4）** 用纯 Python 回退写标签可能错位：内置实现会平移
  `stco`/`co64` 采样偏移，但不处理 `moof`/`tfhd` 的 base-data-offset。
  这类文件请装 mutagen。
- 直播流（无 `#EXT-X-ENDLIST`）会被分片上限拦住，避免无限下载。
- 部分站点有强反爬（验证码、签名校验），需要手动提供 Cookie 或改用其它工具。
- Web 界面的图片预览走本地代理以绕过防盗链，该代理拒绝访问内网地址（防 SSRF）。

---

## 许可

MIT
