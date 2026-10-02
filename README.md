# TubeTape

把家庭照片和视频自动整理成一个个组织好的视频文件，上传到 YouTube 私人频道，用于**备份和观看**。

TubeTape 会扫描一个目录，按拍摄时间把照片/视频紧凑拼接成一个个「分片」视频，通过 ffmpeg 转码后上传到你的 YouTube 私人频道，并加入同一个按时间排序的播放列表。它支持**持续运行**：你把新照片/视频扔进目录，它自动检测、自动归入对应分片、自动（必要时重建并重传）上传。

---

## 目录

- [核心特性](#核心特性)
- [工作原理](#工作原理)
- [安装](#安装)
- [OAuth 配置（一次性）](#oauth-配置一次性)
- [快速开始](#快速开始)
- [命令行参数](#命令行参数)
- [命令例子](#命令例子)
- [长期挂机（持续运行）](#长期挂机持续运行)
- [过滤：只要相机照片和手机视频](#过滤只要相机照片和手机视频)
- [数据库](#数据库)
- [哈希缓存（扫描加速）](#哈希缓存扫描加速)
- [分辨率与画质（保真优先）](#分辨率与画质保真优先)
- [配额](#配额)
- [构建可执行文件](#构建可执行文件)
- [Docker](#docker)
- [常见问题](#常见问题)

---

## 核心特性

- **归档优先**：最终每个文件恰好属于一个分片，分片严格按拍摄时间连续，内容完整。
- **分片不可变 + 按需重建**：YouTube 无法替换已上传视频的文件内容，因此「更新分片」= 先上传新片、成功后删除旧片。
- **分片时间区间固定**：加一张照片只触发它所在的那一个分片重建，不会级联重传。
- **持续监控**：`--watch` 下挂着不用管，新文件自动归入分片、自动上传。
- **来源过滤**：可选择只保留相机拍摄的照片和手机拍摄的视频，丢弃截图和网络传输的压缩副本。
- **配额感知**：以 YouTube API 返回为准，配额用尽自动退避重试，不在本地维护额度计数。
- **上传对账**：上传前列出频道已有视频，按简介里的 `segment_id` 判断；已上传的直接跳过，本地库丢失也不会重复上传。
- **断点续传**：进度存 JSON 库，崩溃/重启后从断点继续，已封口且未变更的分片不重复处理。
- **扫描缓存**：文件「大小 + mtime」未变时直接复用上次的内容哈希和元数据，重复扫描极快。
- **详细日志**：终端友好进度 + `<db>.log` 带时间戳的 DEBUG 审计日志，随时可查「程序正在做什么」。

---

## 工作原理

```
扫描（scan）
  → 递归找图片/视频，提取拍摄时间、分辨率、内容哈希(file_id)
  → 内容哈希只采样头/中/尾各 64KB + 文件大小（小文件整读），不读整个大文件
  → 与数据库对比：新增 / 已处理 / 已删除
  → 哈希缓存：文件「大小 + mtime(ns)」没变时复用上次的哈希和元数据，不重新读文件
  → 视频元数据优先用 ffprobe 结构化读取，缺失时才回退到 ffmpeg -i
  → 扫描中每 1000 个文件（或每 30s）增量落盘，中断不丢已扫进度

分片规划（plan）
  → 按拍摄时间升序，紧凑拼接（去掉时间空洞）
  → 贪心打包，累计时长 ≤ --segment-duration
  → 已有分片的时间区间视为固定边界：新文件落入哪个区间归哪个分片
  → segment_id = sha256(排序后的 file_id + 分片参数)，已存在则跳过

转码（transcode）
  → ffmpeg 转 MP4（H.264 High / yuv420p / AAC 48kHz）
  → 图片转静态帧（可选 Ken Burns），黑边补齐到统一分辨率
  → 章节按文件生成（间隔 <10s 自动合并，第一章 0:00）

上传（upload）
  → 先列出频道已有视频（对账），已在 YouTube 上的分片直接跳过
  → YouTube videos.insert，标题 = {首时间戳} - {末时间戳}
  → 简介写入章节时间戳 + segment_id 标记，privacyStatus、madeForKids、categoryId 显式设置
  → 加入按拍摄时间排序的播放列表，标记 sealed

重建（rebuild）
  → 新文件落入已封口分片区间内 → 转码上传新片 → 校验 → 删除旧片 → 更新库
  → 任何一步失败都保留旧片；旧 video_id 进入 previous_video_ids

监控（watch）
  → watchdog 事件即时发现新文件 + 目录 mtime 兜底扫描（网络盘也能用）
  → 静默期去抖 → 自动重处理；配额用尽则退避重试；退出时 flush 封片
```

---

## 安装

**要求：**

| 依赖 | 说明 |
|---|---|
| Python 3.11+ | （3.10 实测也可运行，但 pyproject 声明 ≥3.11） |
| ffmpeg | 系统命令，提供 `ffmpeg` + `ffprobe`，用于视频元数据探测和转码 |
| 网络 | 访问 YouTube Data API |

```bash
# 1. 克隆仓库
git clone <你的仓库地址>
cd TubeTape

# 2. 安装 Python 依赖
pip install -e ".[dev]"
# 或最小安装：
pip install tzdata Pillow pillow-heif exifread google-api-python-client google-auth-oauthlib watchdog rich

# 3. 确认 ffmpeg 可用
ffmpeg -version
```

> 打包成可执行文件时无需装 Python，但**目标机器仍需安装 ffmpeg**。

**支持的媒体格式：**

| 类型 | 扩展名 |
|---|---|
| 图片 | `.jpg` `.jpeg` `.png` `.heic` `.heif` `.tif` `.tiff` `.webp` `.bmp` |
| 视频 | `.mp4` `.mov` `.m4v` `.avi` `.mkv` `.mts` `.m2ts` `.3gp` |

> HEIC/HEIF 由 `pillow-heif`（pip 依赖，自带 libheif）解码：扫描时读其 EXIF（分辨率/拍摄时间/Make/Model），转码时先转成 PNG 再交给 ffmpeg（ffmpeg 通常不带 HEIC 解码器）。

---

## OAuth 配置（一次性）

YouTube 上传必须 OAuth 2.0，OAuth 客户端只能来自 Google Cloud Console。

1. [Google Cloud Console](https://console.cloud.google.com) 建项目 → 启用 **YouTube Data API v3**。
2. 凭据 → 创建 **OAuth 客户端 ID → 桌面应用** → 下载 `client_secret.json`（放到项目根目录）。
3. OAuth 同意屏幕 → External → 添加自己的邮箱为测试用户 → 加 scope：
   `https://www.googleapis.com/auth/youtube.force-ssl`（覆盖上传/删除/播放列表/读取）。
4. 获取 token（一条命令，打印 URL → 浏览器授权 → 粘贴跳转 URL）：

```bash
python scripts/oauth_login.py --client-secret client_secret.json --output token.json
```

生成的 `token.json` 权限为 0600，含 refresh token。**两个档位：**

- **Testing 模式**：免审核，立即可用，但 token 约 7 天过期，每周重跑一次上面的命令。
- **In production（发布）**：token 长期有效；自己（owner）使用无需 Google 审核。

> ⚠️ `client_secret.json` 和 `token.json` 都含机密，**切勿提交到 git**（已配 `.gitignore`）。

---

## 快速开始

```bash
# 1. 先 dry-run 预览：只看分片计划，不转码不上传
python -m tubetape --dry-run --input /path/to/photos

# 2. 真实运行一次：扫描 → 分片 → 转码 → 上传（私人）
python -m tubetape --input /path/to/photos --timezone Asia/Shanghai

# 3. 持续运行：挂着自动处理新文件
python -m tubetape --input /path/to/photos --watch
```

---

## 命令行参数

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--image-duration` | `3` | 图片转视频时单张图片的播放秒数 |
| `--input` | 当前目录 | 图片/视频根目录 |
| `--db` | `<input>/tubetape.json` | JSON 数据库路径 |
| `--segment-duration` | `1h` | 每片时长上限；支持 `1h` / `3600s` / `1:00:00` |
| `--timezone` | 系统本地 | 无时区 EXIF 时间的解释基准（如 `Asia/Shanghai`） |
| `--crf` | `16` | 视频重编码质量，越小越清晰 |
| `--max-resolution` | `3840x2160` | 分辨率上限，不放大 |
| `--canvas-mode` | `max` | 分片画布尺寸：`max`=所有文件尺寸的包围盒（不降采样，默认），`first`=第一个文件 |
| `--fps` | `60` | 输出帧率（默认 60，保留最高 60fps 源的运动） |
| `--x264-preset` | `slow` | x264 速度/压缩效率档；越慢同码率越清晰 |
| `--ken-burns` | 关 | 图片缩放平移效果（会软化画面，保真建议关） |
| `--privacy` | `private` | `private` 或 `unlisted` |
| `--playlist` | 无 | 追加到的 YouTube 播放列表 ID |
| `--flush` | — | 立即把不足时长的待处理队列强制封片上传 |
| `--watch` / `--no-watch` | 开 | 是否持续监控新文件 |
| `--quiet-period` | `10m` | watch 模式下，最后一次变更后等待多久再处理（防拷贝一半） |
| `--poll-interval` | `30s` | watch 模式内部 tick：多久检查一次时间相关条件 |
| `--mtime-interval` | `1h` | watch 模式下目录 mtime 兜底扫描间隔（补 watchdog/inotify 漏掉的事件） |
| `--quota-backoff` | `1h` | YouTube 报配额用尽后，等待多久再重试 |
| `--only-camera-photos` | 关 | 只保留相机拍摄的照片（EXIF 有 Make+Model） |
| `--only-phone-videos` | 关 | 只保留手机拍摄的视频（元数据有 make） |
| `--dry-run` | — | 只扫描、计算分片和 ID，不转码不上传 |
| `-v` / `--verbose` | 关 | 更详细的终端输出；`-v` 显示 INFO，`-vv` 显示 DEBUG（默认仅 WARNING+） |
| `--log-file` | `<db>.log` | 详细运行日志文件路径（始终记录 DEBUG 级别） |

---

## 命令例子

### 运行日志

程序运行时会输出三层日志：

1. **终端 stdout —— 友好进度**：扫描进度 `scan [i/N] ... [cache]`、分片规划、正在转码第几片、上传结果、配额用量等。
2. **`<db>.log` —— 完整审计日志**：默认写到数据库旁（如 `tubetape.json.log`），始终记录带时间戳的 DEBUG 细节（每个文件的哈希、ffprobe/ffmpeg 命令、上传重试、重建步骤、数据库读写等）。
3. **终端 stderr —— 错误/警告**：默认只显示 WARNING+；加 `-v` 显示 INFO，`-vv` 显示全部 DEBUG。

```bash
# 默认：stdout 友好进度 + <db>.log 详细日志
python -m tubetape --input ~/Photos --no-watch

# 终端也要看全部细节（DEBUG）
python -m tubetape --input ~/Photos --no-watch -vv

# 日志写到指定文件
python -m tubetape --input ~/Photos --no-watch --log-file /var/log/tubetape.log

# 另开终端实时看详细日志（含缓存命中 [cache]）
tail -f ~/Photos/tubetape.json.log
```

> Docker 里：stdout/stderr 用 `docker logs -f tubetape` 看；文件日志在数据库卷里（如 `/db/tubetape.json.log`），可用 `docker exec tubetape tail -f /db/tubetape.json.log` 看。

### 基础用法

```bash
# 预览当前目录（只读，不写库、不转码、不上传）
python -m tubetape --dry-run

# 指定输入目录
python -m tubetape --input ~/Pictures/family --dry-run

# 真实运行（私人，一次性处理完退出）
python -m tubetape --input ~/Pictures/family --no-watch

# 指定时区（中国大陆照片用 Asia/Shanghai）
python -m tubetape --input ~/Pictures/family --timezone Asia/Shanghai
```

### 分片控制

```bash
# 每片最长 5 分钟
python -m tubetape --input ~/Photos --segment-duration 5m --dry-run

# 用秒或时钟格式
python -m tubetape --segment-duration 300s
python -m tubetape --segment-duration 0:05:00

# 图片每张播 5 秒
python -m tubetape --image-duration 5
```

### 持续监控（挂着不管）

```bash
# 后台挂着：初始处理后，自动检测新文件并上传
python -m tubetape --input ~/Photos --watch

# 调快检测（5 秒轮询，1 分钟静默）
python -m tubetape --input ~/Photos --watch --poll-interval 5s --quiet-period 1m

# 后台运行 + 日志
nohup python -m tubetape --input ~/Photos --watch > tubetape.log 2>&1 &
```

### 过滤（只处理相机照片 + 手机视频）

```bash
# 预览会被保留/丢弃的文件
python -m tubetape --input ~/Photos --only-camera-photos --only-phone-videos --dry-run

# 真实运行：丢弃截图和网络传输的压缩副本
python -m tubetape --input ~/Photos --only-camera-photos --only-phone-videos
```

### 转码画质

```bash
# 更高质量（CRF 16）
python -m tubetape --crf 16 --dry-run

# 限制到 1080p
python -m tubetape --max-resolution 1920x1080 --dry-run

# 图片加 Ken Burns 效果
python -m tubetape --ken-burns
```

### 隐私 / 播放列表

```bash
# 上传为 unlisted（知道链接的人可看）
python -m tubetape --privacy unlisted

# 追加到指定播放列表
python -m tubetape --playlist PLxxxxxxxxxxxx
```

### 数据库与配额

```bash
# 自定义数据库路径（默认 <input>/tubetape.json）
python -m tubetape --input ~/Photos --db ~/Photos/tubetape.json

# flush：立即把不足时长的队列封片上传
python -m tubetape --input ~/Photos --flush --no-watch
```

### 用可执行文件（打包后）

```bash
# Linux / macOS
./tubetape --input ~/Photos --dry-run

# Windows
tubetape.exe --input D:\Photos --dry-run
```

---

## 长期挂机（持续运行）

数据量大时（几万张照片/视频），受配额限制（约 6 片/天）需要连续跑很多天。`--watch` 模式就是为「启动后挂着不管」设计的：watchdog 即时发现新文件 + 目录 mtime 兜底扫描，自动归入分片、自动上传；配额用尽则退避后自动重试。

### 完整启动命令

```bash
cd TubeTape
TUBETAPE_TOKEN="$(cat token.json)" nohup python3 -m tubetape \
    --input /path/to/photos \
    --timezone Asia/Shanghai \
    --privacy private \
    --only-camera-photos \
    --only-phone-videos \
    --watch \
    > /tmp/tubetape.log 2>&1 &
```

- 默认每片 `1h`。⚠️ **未验证**的 YouTube 账号单视频上限 15 分钟，需加 `--segment-duration 15m`；已验证账号可用默认 `1h`（或更大）减少总片数。
- `--watch`：持续运行；配额用尽时按 `--quota-backoff`（默认 1 小时）自动重试，不依赖本地额度状态。
- 日志：`tail -f /tmp/tubetape.log`。

### 运行中增加照片/视频会怎样？

挂着的时候往目录里扔新文件，`--watch` 会自动检测（watchdog 即时 + 目录 mtime 兜底，再加 10 分钟静默期去抖，防拷贝一半）并重新处理。新文件按拍摄时间落入三种情况：

| 新文件拍摄时间 | 行为 | 消耗配额 |
|---|---|---|
| 落在**已封口分片**的区间内 | **重建该分片**：转码（并入新文件）→ 上传新片 → 删除旧片 → 更新库（旧 video_id 进 `previous_video_ids`） | 1600 units（一次重传） |
| 落在所有区间**之外** | 进入待处理队列，攒够 `--segment-duration` 才成片上传 | 成片时 1600 units |
| 与现有文件**内容重复**（相同哈希） | 自动去重，忽略 | 0 |

> ⚠️ 给已封口分片「加一张照片」会触发该分片重建：转码上传新片、校验通过后删除旧片，消耗一次配额并改变该片 URL。

### 配额与退避重试

- YouTube 每天约 10000 units ≈ 6 次上传（含重建，具体以 YouTube 为准）。
- 上传被 YouTube 拒绝（`quotaExceeded`）时**优雅暂停本轮**（不崩溃、不丢进度）：已传的分片记录在库，未传的留在队列。
- 等待 `--quota-backoff`（默认 1 小时）后自动重试；配额在 YouTube 侧重置后自动继续，无需人工干预。
- 期间随时 Ctrl+C 退出：会先 flush 封片再退出，进度全部落库。

### 停止与重启

```bash
# 停止（发送退出信号，自动 flush 封片）
kill -INT <pid>          # 或前台运行时按 Ctrl+C

# 重启后从数据库断点续传，已封口未变更的分片不重复处理
TUBETAPE_TOKEN="$(cat token.json)" python3 -m tubetape \
    --input /path/to/photos --timezone Asia/Shanghai --watch
```

### 正式挂机前先 dry-run 看规模

```bash
python3 -m tubetape --dry-run --no-watch --input /path/to/photos \
    --timezone Asia/Shanghai --only-camera-photos --only-phone-videos
```

输出会告诉你：保留/丢弃多少文件、总共多少分片、总时长多少——据此估算要跑多少天。

---

## 过滤：只要相机照片和手机视频

TubeTape 能识别文件「来源」，帮你丢弃截图和网络传输的压缩副本：

| 来源 | 判定依据 |
|---|---|
| 相机照片（保留） | EXIF 有 `Make` + `Model`（如 Apple / iPhone 13） |
| 手机视频（保留） | 视频元数据有 `make`（iPhone/Android 的 `make` 标签） |
| 截图（丢弃） | 无相机 Make/Model |
| 网络传输/下载（丢弃） | 无相机元数据（微信/QQ 传输、网页下载等 EXIF 被剥） |

```bash
python -m tubetape --input ~/Photos --only-camera-photos --only-phone-videos
```

被过滤的文件会打印 `skipped: <路径> (not a camera photo / not a phone video)`。

> 注意：微信/QQ 传过的照片 EXIF 会被剥掉，即使文件名里有日期，也**不算**相机照片。如果你的某批照片只有微信副本，谨慎开启 `--only-camera-photos`，否则它们会被全部丢弃。建议先 `--dry-run` 看保留/丢弃比例再决定。

---

## 数据库

状态存于 `<db>/tubetape.json`，原子写入（临时文件 + rename），崩溃不损坏。结构：

```json
{
  "version": 1,
  "files": { "<file_id>": { "path": "...", "captured_at_utc": "...", "source": "camera", ... } },
  "segments": { "<segment_id>": { "file_ids": [...], "youtube_video_id": "...", "previous_video_ids": [...], "status": "sealed", ... } }
}
```

- `file_id` = 头/中/尾各 64KB + 文件大小的 SHA-256（小文件整读），改路径/改名不变。
- `segment_id` = sha256(排序后的 file_id 列表 + 分片参数)，内容或参数变了才重建。
- `previous_video_ids` = 重建替换掉的旧视频 id。
- `files.*.size_bytes` / `mtime_ns` = 哈希缓存判断依据，扫描时与磁盘 `stat` 比对。

---

## 哈希缓存（扫描加速）

第一次扫描要为每个文件读内容算 SHA-256、并为每个视频跑 ffprobe，数据量大时最慢（几十 GB 视频可能要读很久）。之后每次扫描走缓存，未变文件基本秒过。

### 判断依据（全部满足才命中）

| 维度 | 比较内容 |
|---|---|
| 相对路径 `rel_path` | 作为缓存键，定位「上次这个文件」 |
| 大小 `size_bytes` | 当前 `stat` 大小 == 上次记录 |
| 修改时间 `mtime_ns` | 当前纳秒级 mtime == 上次记录 |
| 类型 `type` | image / video 没变 |

命中后直接复用上次的 `file_id`（内容哈希）、拍摄时间、分辨率、时长、GPS、来源等，**跳过读文件哈希和 ffprobe/EXIF**。日志中命中文件会带 `[cache]`：

```
scan [2/26962] a.jpg (image, 2.3 MB) [cache]
```

### 缓存存在哪里

就存在 `tubetape.json` 的 `files` 记录里（`sha256` + `size_bytes` + `mtime_ns` + 元数据），**没有单独的缓存文件**。

### 中断安全

扫描过程中每 1000 个文件（或每 30 秒）增量保存一次。中途 Ctrl+C / 断电，已扫过的文件已落库，下次扫描直接命中缓存，不用从头再来。

### 注意

- `--dry-run` 不写库，因此**不产生也不更新缓存**；要先生成缓存需真实运行一次。
- 缓存按「路径 + 大小 + mtime」判断，若内容变了但大小和 mtime 被工具刻意保持原样，会误判为未变（标准取舍，家用场景基本不会遇到）。
- 文件移动/改名：路径变了会重新哈希，但算出的 `file_id` 不变，仍会被识别为「已处理」。
- `file_id` 只采样头/中/尾各 64KB + 文件大小；理论上两个大小相同、且这三处内容也完全相同的不同文件会被当成同一个（实际几乎不可能）。可调大代码里的 `HASH_CHUNK` 降低这个概率。

---

## 分辨率与画质（保真优先）

每个分片（按拍摄时间打包、最终拼成一个视频）会统一到一个**画布**尺寸：

- **`--canvas-mode max`（默认）**：画布 = 分片内所有文件尺寸的**包围盒**（每个先按 `--max-resolution` 封顶）→ **任何文件都不会被降采样**。
- `--canvas-mode first`：画布 = 第一个文件（旧行为，可能把后面的高分辨率内容降下来）。
- 每个文件等比缩放到画布并居中：大的缩小、小的放大、比例不同加黑边（`pad`，不裁切）。
- 输出分辨率**永远不超过 `--max-resolution`**（默认 4K）。

保真优先默认值：

| 参数 | 默认 | 作用 |
|---|---|---|
| `--canvas-mode` | `max` | 不丢高分辨率细节 |
| `--max-resolution` | `3840x2160` | 上限 4K（YouTube 对 4K 启用 VP9/AV1） |
| `--crf` | `16` | 高画质（越小越清晰、文件越大） |
| `--x264-preset` | `slow` | 同码率伪影更少，二次编码损失更小 |
| `--fps` | `60` | 不丢 60fps 源的运动细节 |

> ⚠️ 一个视频只能有一个尺寸/宽高比：同一分片里混有不同分辨率/比例的文件时，必然有一方要缩放或加黑边——这是「拼成一个视频」的固有限制。

---

## 配额

- YouTube Data API 每天约 **10000 units**，一次 `videos.insert` 约 **1600 units**（约 6 片/天，以 YouTube 为准）。
- **不在本地维护额度计数**，以 API 返回为准：传不上去（`quotaExceeded`）时本轮立即停止，等 `--quota-backoff`（默认 1 小时）再重试。
- 被拒的请求不消耗配额，所以按小时轮询安全且自愈；配额在 YouTube 侧重置后自动继续。
- 运行日志会打印：`quota exhausted ... deferring remaining N segment(s)` 及每次重试。

---

## 构建可执行文件

### 本机构建（当前操作系统）

```bash
pip install pyinstaller
python scripts/build.py                # 输出 dist/tubetape（Windows 为 tubetape.exe）
python scripts/build.py --name tube    # 自定义名字
```

> PyInstaller 不能交叉编译，只能构建「当前系统」的二进制。

### 三平台构建（GitHub Actions）

推送到 GitHub 后，打 tag 或手动触发 workflow，自动产出 Linux / Windows / macOS 三个二进制：

- 打 tag：`git tag v0.1.0 && git push --tags`
- 或在 GitHub 仓库 Actions 页手动 Run workflow
- 产物在 Actions 的 Artifacts 里下载（`.github/workflows/build.yml`）

> 可执行文件**不捆绑 ffmpeg**，目标机器仍需安装 ffmpeg。

> ⚠️ **glibc 兼容性**：Linux 可执行文件是在 Ubuntu 22.04（glibc 2.35）上构建的，只能在 glibc ≥ 2.35 的系统运行。若在更旧的系统（如 Debian 11）报 `GLIBC_2.xx not found`，请改用源码运行（`python3 -m tubetape`），或自行在目标系统上 `python scripts/build.py` 构建。

---

## Docker

Docker 是**最稳**的运行方式：容器自带 glibc + ffmpeg + 全部 Python 依赖，宿主机只需装 docker，彻底避开可执行文件的 glibc 兼容问题。

**Docker Hub 镜像**：`chet2026/tubetape`

### 拉取镜像

```bash
docker pull chet2026/tubetape
```

### 准备 token

OAuth 拿到的 `token.json` 有两种传入方式，二选一：

- **环境变量**（推荐）：`-e TUBETAPE_TOKEN="$(cat token.json)"`
- **挂载文件**：`-v /path/token.json:/db/token.json`（容器会到 db 同目录找 `token.json`）

### 运行场景

#### 1. 预览（dry-run，只读不传）

```bash
docker run --rm \
    -v ~/projects/u/bone-ash:/data:ro \
    -e TZ=Asia/Shanghai \
    chet2026/tubetape \
    --dry-run --no-watch \
    --input /data --timezone Asia/Shanghai \
    --only-camera-photos --only-phone-videos \
    -v --log-file /tmp/tubetape.log
```

> dry-run 不写库；`/data` 是只读挂载，所以把 `--log-file` 指到容器内的 `/tmp`（或省略 `--log-file` 只看 `-v` 的终端输出）。

#### 2. 长期挂机（推荐：多天自动上传）

```bash
docker run -d --name tubetape \
    --restart unless-stopped \
    -v ~/projects/u/bone-ash:/data:ro \
    -v tubetape-db:/db \
    -e TUBETAPE_TOKEN="$(cat token.json)" \
    -e TZ=Asia/Shanghai \
    chet2026/tubetape \
    --input /data --db /db/tubetape.json \
    --timezone Asia/Shanghai --privacy private \
    --only-camera-photos --only-phone-videos \
    --watch -v
```

> 数据库 `tubetape.json`、哈希缓存、详细日志 `tubetape.json.log` 都在命名卷 `tubetape-db` 里，删容器不丢，重启自动走缓存续传。

#### 3. 一次性处理（处理完退出，不监控）

```bash
docker run --rm \
    -v ~/projects/u/bone-ash:/data:ro \
    -v tubetape-db:/db \
    -e TUBETAPE_TOKEN="$(cat token.json)" \
    -e TZ=Asia/Shanghai \
    chet2026/tubetape \
    --input /data --db /db/tubetape.json \
    --timezone Asia/Shanghai --privacy private --no-watch -v
```

### Docker Compose（推荐：长期挂机）

仓库已带 `docker-compose.yml` 和 `.env.example`，参数与上面的「长期挂机」一致。数据库 `tubetape.json` 和日志 `tubetape.json.log` 会直接生成在 `docker-compose.yml` 同目录下（不用命名卷）。

```bash
cd TubeTape
cp .env.example .env

# 一键生成 .env（含 token；然后手动把 MEDIA_DIR 改成你的照片目录）
{ echo "MEDIA_DIR=/home/user/projects/u/bone-ash"; echo -n "TUBETAPE_TOKEN="; cat token.json; echo; } > .env

# 或直接编辑 .env，填两项：MEDIA_DIR（宿主机绝对路径）、TUBETAPE_TOKEN（token.json 单行内容）
```

```bash
docker compose up -d        # 启动（后台，restart=unless-stopped）
docker compose logs -f      # 实时终端日志
docker compose ps           # 查看运行状态
docker compose down         # 停止并删除容器（tubetape.json / 日志保留在本地，重启走缓存续传）
```

> `.env`、生成的 `tubetape.json`、`tubetape.json.log` 都已加入 `.gitignore`，切勿提交。参数含义见下方「挂载与环境变量」。

### 挂载与环境变量

| 项 | 说明 |
|---|---|
| `-v <照片目录>:/data:ro` | 照片/视频目录**只读**挂载到容器内 `/data` |
| `-v tubetape-db:/db` | （docker run）数据库 + 哈希缓存 + 详细日志存到命名卷；Docker Compose 改用 `./:/db`，json 生成在 compose 同目录 |
| `-e TUBETAPE_TOKEN` | token 环境变量（或挂 `token.json` 到 `/db/token.json`） |
| `-e TZ=Asia/Shanghai` | 容器时区（与 `--timezone` 保持一致） |
| `--restart unless-stopped` | 崩溃/宿主机重启自动拉起，长期挂机必备 |
| `-v` / `-vv` | 终端显示 INFO / DEBUG（透传参数） |
| `--log-file <path>` | 详细日志路径；默认 `<db>.log`（即 `/db/tubetape.json.log`） |

> 注意：容器内 `--input` 写挂载路径 `/data`，不是宿主机路径。其它 CLI 参数原样透传。

### 日志 / 停止 / 更新

```bash
docker logs -f tubetape                 # 实时终端日志（stdout/stderr）
docker logs --tail 200 tubetape         # 最近 200 行
docker exec tubetape tail -f /db/tubetape.json.log   # 实时详细 DEBUG 日志（含 [cache]）

docker stop tubetape                    # 停止（SIGTERM，会自动 flush 封片再退出）
docker start tubetape                   # 再次启动（续传，走哈希缓存）
docker rm tubetape                      # 删除容器（数据卷 tubetape-db 保留）

# 更新到新版本
docker pull chet2026/tubetape
docker stop tubetape && docker rm tubetape
# 然后重新 docker run（同上命令）
```

### 自己构建并推送到 Docker Hub

脚本默认在构建完成后直接推送到 `chet2026/tubetape`（可用 `TUBETAPE_IMAGE` 环境变量覆盖）。推送前先 `docker login`。

```bash
./scripts/docker_build.sh                     # 构建并推送 chet2026/tubetape:latest（当前架构）
./scripts/docker_build.sh v0.1.1              # 构建并推送指定 tag
./scripts/docker_build.sh v0.1.1 --no-push    # 只构建不推送（本地测试）
./scripts/docker_build.sh latest --multi      # 多架构 amd64+arm64 构建并推送（需 buildx）
TUBETAPE_IMAGE=myhub/tubetape ./scripts/docker_build.sh v0.1.1  # 推送到自定义仓库
```

---

## 常见问题

**Q：上传报 `youtubeSignupRequired`？**
你的 Google 账号还没有 YouTube 频道。去 youtube.com 用同一账号创建频道即可。

**Q：报 `Scope has changed` / OAuth 失败？**
这是增量授权导致返回的 scope 比请求的多。项目已设置 `OAUTHLIB_RELAX_TOKEN_SCOPE=1` 自动放行；若还报错，删掉 `token.json` 重新 `oauth_login.py`。

**Q：报 `insufficientPermissions` 删不了视频？**
旧 token 缺 `youtube.force-ssl` scope。重新走 OAuth 拿新 token。

**Q：照片时间全变成拷贝时间？**
EXIF 被剥（微信/QQ 传输）时会优先用文件名里的时间（如 `2020_12_16_01_00_IMG_6558.JPG`），再不行才用文件 mtime。可用 `--timezone` 指定正确时区。

**Q：dry-run 会不会写库/上传？**
不会。dry-run 完全只读，不写数据库、不转码、不上传。

**Q：怎么把已上传的测试视频删掉？**
用 `--privacy private` 上传后，在 YouTube Studio → 内容里手动删除；或用有 `youtube.force-ssl` 权限的脚本调 `videos().delete()`。

**Q：配额不够怎么办？**
默认约 6 片/天。减少分片数（增大 `--segment-duration`），或等次日自动恢复。

---

## 更新记录

- **保真优先默认值**：`--canvas-mode max`（画布取分片包围盒，不降采样）、`--crf 16`、`--x264-preset slow`、`--fps 60`；新增 `--canvas-mode` / `--x264-preset` / `--fps` 三个开关。
- **上传对账**：上传前用 `channels.list` + `playlistItems.list` 列出频道已有视频，按简介里的 `segment_id` 标记匹配；已上传的直接跳过并补写本地记录（本地库丢失也不会重复上传）。不做自动删除。
- **配额改为 API 驱动**：去掉本地额度计数与持久化，YouTube 返回 `quotaExceeded` 就暂停本轮，等 `--quota-backoff`（默认 1h）自动重试。
- **watch 改为事件驱动**：watchdog（inotify/FSEvents）即时发现新文件 + 目录 mtime 兜底扫描（`--mtime-interval` 默认 1h），替代原来每 30s 全量遍历文件。
- **HEIC/HEIF 支持**：扫描时读取 HEIC/HEIF 的 EXIF（分辨率/拍摄时间/Make/Model），转码前先转成 PNG 再交给 ffmpeg（ffmpeg 通常不带 HEIC 解码器）。新增 `.heif`（图片）和 `.3gp`（视频）格式。
- **快速采样哈希**：`file_id` 改为「头/中/尾各 64KB + 文件大小」的 SHA-256（小文件整读），首次扫描读取量从 ~百 GB 降到 ~5GB，实测冷缓存约 10 倍、热缓存约 67 倍提速。
- **哈希缓存**：按「路径 + 大小 + mtime(ns)」复用上次哈希和元数据，重复扫描几乎瞬时；扫描过程中每 1000 文件/30s 增量落盘，中断不丢已扫进度。
- **详细日志**：终端友好进度 + `<db>.log` 带时间戳 DEBUG 审计日志；`-v`/`-vv` 控制终端详细度，`--log-file` 指定日志路径。
- **ffprobe 探测**：视频元数据优先用 `ffprobe` 结构化读取，失败时回退 `ffmpeg -i`。
- **Docker Compose**：新增 `docker-compose.yml` + `.env.example`；数据库/哈希缓存/日志落在 compose 同目录；构建脚本默认构建并推送 `chet2026/tubetape`。

---

## 许可证

（按需填写，例如 MIT）
