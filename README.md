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
- **配额感知**：每日配额用量持久化，超出自动排队到次日。
- **断点续传**：进度存 JSON 库，崩溃/重启后从断点继续，已封口且未变更的分片不重复处理。

---

## 工作原理

```
扫描（scan）
  → 递归找图片/视频，提取拍摄时间、分辨率、内容哈希(file_id)
  → 与数据库对比：新增 / 已处理 / 已删除

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
  → YouTube videos.insert，标题 = {首时间戳} - {末时间戳}
  → 简介写入章节时间戳，privacyStatus、madeForKids、categoryId 显式设置
  → 加入按拍摄时间排序的播放列表，标记 sealed

重建（rebuild）
  → 新文件落入已封口分片区间内 → 转码上传新片 → 校验 → 删除旧片 → 更新库
  → 任何一步失败都保留旧片；旧 video_id 进入 previous_video_ids

监控（watch）
  → 轮询新文件 → 静默期去抖 → 自动重处理；退出时 flush 封片
```

---

## 安装

**要求：**

| 依赖 | 说明 |
|---|---|
| Python 3.11+ | （3.10 实测也可运行，但 pyproject 声明 ≥3.11） |
| ffmpeg | 系统命令，用于解析视频元数据和转码 |
| 网络 | 访问 YouTube Data API |

```bash
# 1. 克隆仓库
git clone <你的仓库地址>
cd TubeTape

# 2. 安装 Python 依赖
pip install -e ".[dev]"
# 或最小安装：
pip install tzdata Pillow exifread google-api-python-client google-auth-oauthlib watchdog rich

# 3. 确认 ffmpeg 可用
ffmpeg -version
```

> 打包成可执行文件时无需装 Python，但**目标机器仍需安装 ffmpeg**。

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
| `--crf` | `18` | 视频重编码质量，越小越清晰 |
| `--max-resolution` | `3840x2160` | 目标分辨率上限，不放大 |
| `--ken-burns` | 关 | 图片缩放平移效果 |
| `--privacy` | `private` | `private` 或 `unlisted` |
| `--playlist` | 无 | 追加到的 YouTube 播放列表 ID |
| `--no-rebuild` | 关 | 新文件落入已封口分片时不重建，改为单独补录分片 |
| `--rebuild-cooldown` | `24h` | 同一分片两次重建的最短间隔 |
| `--flush` | — | 立即把不足时长的待处理队列强制封片上传 |
| `--watch` / `--no-watch` | 开 | 是否持续监控新文件 |
| `--quiet-period` | `10m` | watch 模式下，最后一次变更后等待多久再处理（防拷贝一半） |
| `--poll-interval` | `30s` | watch 模式下轮询新文件的间隔 |
| `--only-camera-photos` | 关 | 只保留相机拍摄的照片（EXIF 有 Make+Model） |
| `--only-phone-videos` | 关 | 只保留手机拍摄的视频（元数据有 make） |
| `--dry-run` | — | 只扫描、计算分片和 ID，不转码不上传 |

---

## 命令例子

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

### 隐私 / 播放列表 / 重建

```bash
# 上传为 unlisted（知道链接的人可看）
python -m tubetape --privacy unlisted

# 追加到指定播放列表
python -m tubetape --playlist PLxxxxxxxxxxxx

# 不重建已封口分片（新文件单独成片）
python -m tubetape --no-rebuild

# 重建冷却调到 12 小时
python -m tubetape --rebuild-cooldown 12h
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

数据量大时（几万张照片/视频），受配额限制（约 6 片/天）需要连续跑很多天。`--watch` 模式就是为「启动后挂着不管」设计的：自动检测新文件、自动归入分片、自动上传，配额耗尽自动暂停、每日自动续传。

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
- `--watch`：持续运行；配额耗尽后自动暂停，每天（UTC 零点）自动续传。
- 日志：`tail -f /tmp/tubetape.log`。

### 运行中增加照片/视频会怎样？

挂着的时候往目录里扔新文件，`--watch` 会自动检测（默认每 30s 轮询 + 10 分钟静默期去抖，防拷贝一半）并重新处理。新文件按拍摄时间落入三种情况：

| 新文件拍摄时间 | 行为 | 消耗配额 |
|---|---|---|
| 落在**已封口分片**的区间内 | **重建该分片**：转码（并入新文件）→ 上传新片 → 删除旧片 → 更新库（旧 video_id 进 `previous_video_ids`） | 1600 units（一次重传） |
| 落在所有区间**之外** | 进入待处理队列，攒够 `--segment-duration` 才成片上传 | 成片时 1600 units |
| 与现有文件**内容重复**（相同哈希） | 自动去重，忽略 | 0 |

> ⚠️ 给已封口分片「加一张照片」会触发该分片重建，消耗一次配额并改变该片 URL。可用 `--no-rebuild` 改为单独补录分片（不动旧片、不删旧视频）。
> 默认有 24h 冷却（`--rebuild-cooldown`），同一分片多次变更会合并成一次重建。

### 配额与每日续传

- 每天约 10000 units ≈ 6 次上传（含重建）。
- 用完当天配额后**优雅暂停**（不崩溃、不丢进度）：已传的分片记录在库，未传的留在队列。
- watch 每天自动重新处理，继续传剩余分片，直到全部传完。
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
  "segments": { "<segment_id>": { "file_ids": [...], "youtube_video_id": "...", "previous_video_ids": [...], "status": "sealed", ... } },
  "queue": { "pending_file_ids": [], "rebuild_segment_ids": [] },
  "errors": [],
  "settings": { "quota": { "date": "2026-10-01", "used": 3200 } }
}
```

- `file_id` = 文件内容的 SHA-256（>512MB 用快速哈希），改路径/改名不变。
- `segment_id` = sha256(排序后的 file_id 列表 + 分片参数)，内容或参数变了才重建。
- `previous_video_ids` = 重建替换掉的旧视频 id。
- `settings.quota` = 当日配额用量，跨运行持久化，每日自动归零。

---

## 配额

- 每次上传（含重建）消耗 **1600 units**，默认每日 **10000 units**（约 6 片/天）。
- 用量持久化到 `settings.quota`，重启不丢失。
- 超出后上传会抛错并排队到次日（`QuotaExceededError`）。
- 运行日志会打印：`quota: 1600/10000 units (8400 remaining)`。

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
    --only-camera-photos --only-phone-videos
```

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
    --watch
```

#### 3. 一次性处理（处理完退出，不监控）

```bash
docker run --rm \
    -v ~/projects/u/bone-ash:/data:ro \
    -v tubetape-db:/db \
    -e TUBETAPE_TOKEN="$(cat token.json)" \
    -e TZ=Asia/Shanghai \
    chet2026/tubetape \
    --input /data --db /db/tubetape.json \
    --timezone Asia/Shanghai --privacy private --no-watch
```

### 挂载与环境变量

| 项 | 说明 |
|---|---|
| `-v <照片目录>:/data:ro` | 照片/视频目录**只读**挂载到容器内 `/data` |
| `-v tubetape-db:/db` | 数据库存到 Docker 命名卷（持久化，删容器不丢） |
| `-e TUBETAPE_TOKEN` | token 环境变量（或挂 `token.json` 到 `/db/token.json`） |
| `-e TZ=Asia/Shanghai` | 容器时区（与 `--timezone` 保持一致） |
| `--restart unless-stopped` | 崩溃/宿主机重启自动拉起，长期挂机必备 |

> 注意：容器内 `--input` 写挂载路径 `/data`，不是宿主机路径。其它 CLI 参数原样透传。

### 日志 / 停止 / 更新

```bash
docker logs -f tubetape     # 实时日志
docker logs --tail 200 tubetape   # 最近 200 行

docker stop tubetape        # 停止（SIGTERM，会自动 flush 封片再退出）
docker start tubetape       # 再次启动（续传）
docker rm tubetape          # 删除容器（数据卷 tubetape-db 保留）

# 更新到新版本
docker pull chet2026/tubetape
docker stop tubetape && docker rm tubetape
# 然后重新 docker run（同上命令）
```

### 自己构建镜像

```bash
./scripts/docker_build.sh               # 构建 tubetape:latest（当前架构）
./scripts/docker_build.sh v0.1.1        # 指定 tag
./scripts/docker_build.sh latest --multi  # 多架构 amd64 + arm64（需 buildx）
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

## 许可证

（按需填写，例如 MIT）
