# TubeTape 开发里程碑

> 本文档是 `prompt.md` 的**可执行拆分与持续演进记录**。
> 贯穿全项目的工程标准：正确性优先、测试全绿、画质保真优先、不可变分片原子重建与云端容灾。

## 总览

| 里程碑 | 主题 | 关键交付 | 新增依赖 |
|---|---|---|---|
| M0 | 项目骨架 + CLI 参数解析 | `cli.py`、`durations.py`、`test_cli.py`、`test_durations.py`、`__main__.py` | pytest |
| M1 | 数据库 schema + 原子读写 | `db.py`、`test_db.py` | — |
| M2 | 扫描与建库 + 采样哈希与缓存 | `scanner.py`、`test_scanner.py`、`conftest.py` | Pillow、pillow-heif、exifread、ffprobe/ffmpeg（系统） |
| M3 | 分片规划 | `planner.py`、`test_planner.py` | — |
| M4 | 转码 + 章节计算 + 画质保真 | `transcoder.py`、`chapters.py`、`test_transcoder.py`、`test_chapters.py` | ffmpeg（系统） |
| M5 | OAuth 登录 + 上传 + 播放列表 + 配额 | `auth.py`、`uploader.py`、`test_auth.py`、`test_uploader.py` | google-api-python-client、google-auth-oauthlib |
| M6 | 重建 + 动态监控 + flush + 优雅停机 | `rebuild.py`、`watcher.py`、`test_rebuild.py`、`test_watcher.py` | watchdog |
| M7 | 进度展示 + 集成验证 + 统一日志 | `ui.py`、`log.py`、`test_ui.py`、`cli.py` 串联全流程 | rich |
| M8 | 文件名时间兜底 | `scanner.py`（`parse_filename_time`） | — |
| M9 | 上传对账与云端容灾 | `reconcile.py`、`test_reconcile.py` | — |
| M10 | 容器化、跨平台打包与运维发布 | `Dockerfile`、`docker-compose.yml`、`tubetape.spec`、`scripts/`、`docs/` | PyInstaller（build） |
| M11 | Web 实时控制台、OAuth 自愈与分块断点续传 | `web.py`、`uploader.py`、`auth.py`、`test_web.py` | — |
| M12 | 方案 A 一体化 Web OAuth 授权流 | `auth.py`、`web.py`、`cli.py`、`test_auth.py`、`test_web.py` | — |

## 目录结构

```
TubeTape/
├── prompt.md            # 原始需求规格（只读）
├── development.md       # 本文件：里程碑拆分与架构演进
├── pyproject.toml       # 项目配置、依赖与 pytest 设置
├── Dockerfile           # 生产容器镜像构建
├── docker-compose.yml   # 容器化挂载运行配置
├── tubetape.spec        # PyInstaller 单二进制打包规格
├── tubetape/
│   ├── __init__.py
│   ├── __main__.py      # 支持 python -m tubetape
│   ├── cli.py           # 入口 + 完整参数解析 + 管道流程调度
│   ├── durations.py     # 时长 / 分辨率 / 时区解析（M0）
│   ├── db.py            # 数据库 + 原子读写 + 缓存管理（M1）
│   ├── scanner.py       # 扫描 + 元数据提取 + 采样哈希 + 来源过滤（M2、M8）
│   ├── planner.py       # 分片规划 + 固定区间分配 + 标题生成（M3）
│   ├── transcoder.py    # 转码 + 动态画布 + HEIF转换 + ffmpeg concat（M4）
│   ├── chapters.py      # 视频章节计算与文本生成（M4）
│   ├── auth.py          # Google OAuth 凭据加载 + 内置登录 flow（M5）
│   ├── uploader.py      # 可断点上传 + 播放列表维护 + 配额感知（M5）
│   ├── rebuild.py       # 不可变分片原子重建（先传新片再删旧片）（M6）
│   ├── watcher.py       # watchdog 事件监控 + mtime 目录轮询兜底（M6）
│   ├── ui.py            # rich 交互式进度 + 非交互文本兼容（M7）
│   ├── log.py           # 集中式日志：控制台 + <db>.log 审计追踪（M7）
│   └── reconcile.py     # 云端 uploads 列表对账与分片标记注入（M9）
├── tests/
│   ├── conftest.py      # 测试媒体合成 fixture（jpg/exif、heic、mov/mp4等）
│   ├── test_cli.py      # CLI 参数与端到端 dry-run 测试
│   ├── test_durations.py# 格式解析单测
│   ├── test_db.py       # 数据库读写、原子性与损坏容错单测
│   ├── test_scanner.py  # 扫描器、哈希、EXIF/ffprobe解析与时间来源单测
│   ├── test_planner.py  # 贪心分片、固定区间与 flush 单测
│   ├── test_transcoder.py # 转码命令与 ffmpeg 冒烟测试
│   ├── test_chapters.py # 章节计算逻辑单测
│   ├── test_auth.py     # OAuth 流程与凭据解析单测
│   ├── test_uploader.py # 上传重试、配额识别与播放列表单测
│   ├── test_rebuild.py  # 原子重建状态机与容错单测
│   ├── test_watcher.py  # 文件监控与目录 mtime 扫描单测
│   ├── test_ui.py       # Reporter 终端输出单测
│   └── test_reconcile.py# 上传对账与 segment_id 标记注入/提取单测
├── scripts/
│   ├── oauth_login.py   # 独立 OAuth 登录脚本
│   ├── upload_one.py    # 单视频冒烟上传调试脚本
│   ├── build.py         # PyInstaller 二进制构建脚本
│   └── docker_build.sh  # Docker 多平台镜像构建脚本
└── docs/                # GitHub Pages 站点与 Google 验证用条款
    ├── _config.yml
    ├── index.md
    ├── Terms_of_Service.md
    └── Privacy_Policy.md
```

## 贯穿性约定（全项目强制遵守）

1. **时间戳与来源优先级**：
   - 内部/排序统一使用 UTC epoch 秒，存储统一采用 ISO 8601 `YYYY-MM-DDTHH:MM:SSZ`；界面展示与分片命名使用 UTC `YYYYMMDD-HHMMSS`。
   - 拍摄时间来源优先级（M8 扩展）：视频 `creation_time`（优先 UTC） → 照片 EXIF `DateTimeOriginal`（按 `--timezone` 解释） → **文件名时间模式**（按 `--timezone` 解释） → 文件 mtime 兜底。
   - 文件记录中用 `time_source` 字段标明 `metadata | filename | mtime`；若为文件名或 mtime 兜底，`missing_meta` 标为 `true`。
   - 支持相机/手机来源识别：`source` 标为 `camera | other`，用于 `--only-camera-photos` 与 `--only-phone-videos` 过滤。
2. **file_id 与高速哈希缓存**：
   - 文件大小 ≤ 192 KB（即 3 × 64 KB）：全量 `sha256(内容)`。
   - 文件大小 > 192 KB：采样哈希 `sha256(前 64KB + 中 64KB + 尾 64KB + 文件大小)`，使百万级大库初始扫描从 $O(\text{总字节数})$ 降为 $O(\text{文件数})$。
   - 数据库记录文件的 `size_bytes` 与 `mtime_ns`。当文件大小与纳秒级修改时间均未变时，直接复用已有哈希与元数据，跳过内容哈希与 ffprobe/exif 解析。
   - 扫描过程中支持增量保存（每 1000 个文件或每 30 秒自动落盘一次），确保海量扫描被中断时保留进度。
3. **segment_id**：
   - `sha256(排序后的 file_id 列表 + 分片参数)`。
   - 分片参数包含 `(image_duration, crf, max_resolution, ken_burns, canvas_mode, fps, x264_preset)` 7 项配置。
   - 包含内容变动或转码参数调整均会导致 `segment_id` 改变，从而触发重建/重传；不变则完全跳过。
4. **分片命名与视频标题**：
   - 格式统一为 `{首文件时间戳} - {末文件时间戳} [{short_id}]`（如 `20240101-153000 - 20240101-160000 [a1b2c3d4e5f67890]`）。
   - 将 16 位分片短哈希内嵌于标题尾部，确保云端通过 `playlistItems.list` 读取时绝不发生如简介一般的截断问题。
   - 视频简介采用纯文件时间戳格式（如 `0:00 20240101-153000\n0:15 ...`），支持点击直接跳到对应素材。
5. **数据库原子写**：
   - 先写同目录 `.tubetape-*.tmp` 临时文件，`os.fsync` 后通过 `os.replace` 原子替换原文件，崩溃绝不损坏数据；状态变更即时落盘。
6. **分片不可变与原子重建**：
   - 紧凑拼接（不填补时间空洞）；YouTube 已上传视频不可就地修改。
   - 已有分片时间区间 `[start_ts, end_ts]` 视为固定边界，已有分片内的文件严格绑定其原分片，杜绝同秒照片边界碰撞导致的重复循环。
   - 重建顺序必须严格执行：「转码新片 → 上传新片 → 校验时长与章节 → 删除旧视频 → 提交新分片至数据库」。删除旧视频失败仅打 Warning 并继续提交，绝不阻断新片提交造成重复循环。
7. **上传对账与云端容灾（M9）**：
   - 启动正式上传前，自动拉取频道 uploads 播放列表。
   - 优先通过标题中的 `[{short_id}]` 进行精准匹配（简介 Marker 作为向下兼容兜底）。
   - 若云端已存在该分片，直接跳过转码与上传并登记本地数据库；若为重建分片且云端已存在新片，则清理旧记录，保障绝对幂等。
8. **分块断点续传（Resumable Chunked Upload）**：
   - 采用 10MB 分块（`next_chunk()`）执行网络传输，具备指数退避重试，杜绝网络波动整片重传导致的重复建片。
9. **画质保真优先转码默认值**：
   - CRF 默认 `16`、预设 `slow`、帧率 `60fps`、像素格式 `yuv420p`、音频统一 `48kHz 2ch AAC 128k`。
   - 画布尺寸默认 `max`（分片内所有素材的最大包围盒），分辨率上限默认 `7680x4320`（8K上限，不降采样，非 8K 素材保持原始尺寸，绝不盲目插值放大）。
   - HEIC/HEIF 图片自动根据 EXIF 方向校正旋转后转为 PNG 送交 ffmpeg。
9. **测试覆盖与隔离**：
   - 单测基于 `tests/conftest.py` 生成的小型合成样本（几像素图片 + 1~2 秒视频），220+ 项测试始终保持全绿。
   - 严禁对实际大库运行未加 `--dry-run` 的写入/上传测试。

---

## M0：项目骨架 + CLI 参数解析

**目标**：搭好可运行、可测试的骨架，完整解析启动参数，支持默认值计算与校验，支持 `python -m tubetape` 运行。

**新增依赖**：pytest（dev）。

**交付**：`pyproject.toml`、`tubetape/__init__.py`、`tubetape/__main__.py`、`tubetape/cli.py`、`tubetape/durations.py`、`tests/test_cli.py`、`tests/test_durations.py`。

**任务清单**：
1. `pyproject.toml`：声明 `requires-python >= 3.11`，配置 `[project.scripts] tubetape = "tubetape.cli:main"`，指定 pytest 配置。
2. 完整定义全部 25 个 CLI 命令行参数，默认值精确对应系统实际运行参数：

   | 参数 | 默认值 | 说明 |
   |---|---|---|
   | `--image-duration` | `3.0` | 图片转视频时单张展示秒数 |
   | `--input` | `.`（当前目录） | 图片与视频根目录 |
   | `--db` | `<input>/tubetape.json` | 数据库路径（运行时根据 input 动态拼接） |
   | `--segment-duration` | `1h`（3600秒） | 每片目标累计时长上限 |
   | `--timezone` | 系统本地时区 | 无时区 EXIF 时间的解释基准 |
   | `--crf` | `16` | 视频重编码画质（保真优先） |
   | `--max-resolution` | `7680x4320` | 分辨率包围盒上限（8K） |
   | `--canvas-mode` | `max` | 分片画布计算模式（`max`: 包围盒不降采样，`first`: 第一张图） |
   | `--fps` | `60` | 输出帧率（保留高达 60fps 的流畅度） |
   | `--x264-preset` | `slow` | x264 编码速度/效率档 |
   | `--ken-burns` | 关 | 图片缩放平移效果（默认关以保真） |
   | `--privacy` | `private` | YouTube 隐私状态（private / unlisted） |
   | `--playlist` | 无 | 追加到的 YouTube 播放列表 ID |
   | `--flush` | 关 | 强制封口并上传待处理队列 |
   | `--watch` / `--no-watch` | 默认开（`BooleanOptionalAction`） | 是否持续监控新文件 |
   | `--quiet-period` | `10m` | watch 模式下最后一次变更后等待静默期 |
   | `--poll-interval` | `30s` | watch 模式内部 tick 轮询间隔 |
   | `--mtime-interval` | `1h` | 目录 mtime 兜底扫描间隔 |
   | `--quota-backoff` | `1h` | YouTube API 报配额耗尽后退避等待时间 |
   | `--only-camera-photos` | 关 | 仅保留相机照片（EXIF 具备 Make+Model） |
   | `--only-phone-videos` | 关 | 仅保留手机视频（元数据具备 make） |
   | `--dry-run` | 关 | 仅预览分片与 ID，不转码不上传不写库 |
   | `-v` / `--verbose` | `0` | 控制台详细度（`-v` INFO，`-vv` DEBUG） |
   | `--log-file` | `<db>.log` | 详细 DEBUG 审计日志文件路径 |
   | `--login` | 关 | 启动 Google OAuth 授权流程生成 `token.json` 后退出 |
   | `--client-secret` | `<db目录>/client_secret.json` | `--login` 所用的凭据文件路径 |
   | `--web-port` | `8080` | Web 实时控制台与日志查看端口（0为禁用） |

3. `durations.py`：
   - `parse_duration("1h" | "20m" | "1200s" | "0:20:00" | "1200") -> float 秒`，非法输入抛出清晰错误。
   - `parse_resolution("7680x4320") -> (w, h)`，非法报错。
   - `parse_timezone("Asia/Shanghai") -> zoneinfo.ZoneInfo`，支持 IANA 时区与本地时区获取。
4. `cli.py`：暴露 `build_parser()` 与 `parse_args(argv=None) -> Namespace`，执行路径绝对化与数值范围后置校验（如 `crf >= 0`、`fps > 0` 等）。
5. 退出码规范：`_EXIT_OK = 0`，`_EXIT_ERROR = 1`，`_EXIT_QUOTA = 2`。

**验收标准**：
- `test_cli.py` 覆盖所有参数的默认值、显式传值及非法值报错。
- `test_durations.py` 覆盖各时长格式、分辨率与时区边界情况。
- `pytest` 全部通过。

---

## M1：数据库 schema + 原子读写

**目标**：实现 JSON 数据库 schema，原子写入、数据持久化与损坏隔离。

**新增依赖**：无（Python 标准库 `json`、`tempfile`、`os`）。

**交付**：`tubetape/db.py`、`tests/test_db.py`。

**任务清单**：
1. `Database` 类：`load(path)` / `save(path)` / 字段访问器，`version: 1`。
2. Schema 结构：
   - `files` 字典：
     ```json
     {
       "path": "relative/path.jpg",
       "name": "path.jpg",
       "type": "image | video",
       "captured_at_utc": "2024-01-01T15:30:00Z",
       "resolution": "4000x3000",
       "duration_seconds": 3.0,
       "size_bytes": 1234567,
       "sha256": "...",
       "location": {"lat": 0.0, "lng": 0.0},
       "missing_meta": false,
       "time_source": "metadata | filename | mtime",
       "source": "camera | other",
       "mtime_ns": 1700000000000000000
     }
     ```
   - `segments` 字典：
     ```json
     {
       "file_ids": ["..."],
       "range": ["2024-01-01T15:30:00Z", "2024-01-01T15:40:00Z"],
       "duration_seconds": 600.0,
       "output_path": "...",
       "youtube_video_id": "...",
       "previous_video_ids": ["..."],
       "status": "sealed | failed",
       "chapters": [["0:00", "20240101-153000"]],
       "last_rebuilt_at": "2024-01-01T16:00:00Z",
       "attempts": 0,
       "error": null
     }
     ```
3. 原子写入：向同目录 `.tubetape-*.tmp` 写入 JSON，经 `os.fsync` 后通过 `os.replace` 重命名覆盖目标文件。
4. 损坏容错：JSON 格式损坏时抛出 `DatabaseError` 并报错，不静默覆盖损毁原库。
5. 提供辅助方法：`upsert_file`、`remove_file`、`get_segment`、`upsert_segment`、`to_dict`。

**验收标准**：
- 新建 / 读取 / 保存往返数据完全一致。
- 模拟中断写入时不破坏已有数据库。
- `test_db.py` 覆盖 schema 全部字段与异常路径。

---

## M2：扫描与建库 + 采样哈希与缓存

**目标**：扫描输入目录，提取照片/视频元数据与采样哈希，实现基于大小和 mtime 的哈希缓存及增量持久化，支持来源过滤。

**新增依赖**：Pillow、pillow-heif、exifread；ffprobe / ffmpeg（系统）。

**交付**：`tubetape/scanner.py`、`tests/test_scanner.py`、`tests/conftest.py`。

**任务清单**：
1. 扩展名覆盖：
   - 图片：`.jpg, .jpeg, .png, .heic, .heif, .tif, .tiff, .webp, .bmp`。
   - 视频：`.mp4, .mov, .m4v, .avi, .mkv, .mts, .m2ts, .3gp`。
   - 注册 `pillow_heif`，让 Pillow 原生解码 HEIC/HEIF 并读取 EXIF 与 GPS。
2. 采样哈希 `file_sha256`：
   - 默认分块 `HASH_CHUNK = 64 * 1024`（64 KB）。
   - 文件大小 ≤ 3 块（192 KB）全量哈希；大于 192 KB 读取前 64KB + 中 64KB + 尾 64KB + 文件大小拼接哈希。
3. 扫描缓存机制：
   - 传入数据库现有文件映射 `cache`。
   - 当文件的 `size_bytes` 与 `st_mtime_ns` 均与缓存匹配时，直接复用 `file_id` 与已解析的元数据，完全跳过哈希与 ffprobe/EXIF。
4. 增量保存支持：
   - `scan_files` 接收 `on_file` 回调，每扫描/命中一个文件触发，CLI 中每 1000 个文件或每 30 秒执行一次 `db.save()`。
5. 视频探测：
   - 优先通过 `ffprobe -v error -print_format json -show_format -show_streams` 获取精准时长、分辨率、`creation_time` 与相机 make 标签。
   - ffprobe 缺失或失败时平滑降级为 `ffmpeg -hide_banner -i` 解析 stderr。
6. 来源过滤：
   - `--only-camera-photos`：检查照片是否具备 EXIF `Make` 与 `Model`。
   - `--only-phone-videos`：检查视频元数据是否具备 `make` 标签。
   - 不符合条件的文件放入 `skipped` 列表，损坏文件记录到 `errors`。

**验收标准**：
- 采样哈希稳定性：修改路径不改变 ID，修改内容产生新 ID。
- 缓存命中：第二次扫描相同文件不触发文件读取与 ffprobe。
- HEIC/HEIF 正确解析元数据、GPS 与拍摄时间。
- 损坏文件记录至 errors 且不中断扫描。
- `test_scanner.py` 全部通过。

---

## M3：分片规划

**目标**：按拍摄时间将文件规划为紧凑分片；后续运行时锁定已有分片边界，单片超限独立成片，计算包含转码参数的 `segment_id`。

**新增依赖**：无。

**交付**：`tubetape/planner.py`、`tests/test_planner.py`。

**任务清单**：
1. 排序：以 `captured_epoch` 升序为主、`file_id` 为辅进行确定性排序。
2. 首次运行：贪心打包，累计内容时长 $\le$ `--segment-duration`；单文件时长超过分片上限时不切割，独立成片。
3. 增量运行（区间固定）：
   - 已有分片的 `[start_ts, end_ts]` 构成闭区间固定边界。
   - 新扫描文件落入已有区间时，归入对应分片标记重建；区间之外的文件按贪心打包规划为新分片。
4. `segment_id` 计算：
   - `segment_id = sha256(sorted(file_ids) + params)`。
   - `params` 包含 `(image_duration, crf, max_resolution, ken_burns, canvas_mode, fps, x264_preset)`。
   - 若数据库中已存在相同 `segment_id`，则纳入 `skipped_segment_ids` 跳过处理。
5. 尾部分片暂存与 flush：
   - 累计时长不足 `--segment-duration` 且未开启 `flush` 的分片存入 `pending_files` 暂存。
   - 开启 `--flush` 或在退出信号时强制将暂存文件打包成片。
6. 标题生成：`{首文件时间} - {末文件时间}`（UTC `YYYYMMDD-HHMMSS`）。

**验收标准**：
- 贪心分片边界切分准确。
- 区间内增补文件只触发目标分片重建，区间外文件生成新分片。
- 修改转码参数会导致 `segment_id` 变更。
- flush 状态切换与单大视频处理符合预期。

---

## M4：转码 + 章节计算 + 画质保真

**目标**：调用 ffmpeg 执行保真优先的中间片段转码与无损拼接，生成精确对应的章节信息。

**新增依赖**：ffmpeg（系统）。

**交付**：`tubetape/transcoder.py`、`tubetape/chapters.py`、`tests/test_transcoder.py`、`tests/test_chapters.py`。

**任务清单**：
1. 画质与编码参数：
   - 编码器 `libx264`，预设 `--x264-preset`（默认 `slow`），CRF `--crf`（默认 `16`），帧率 `--fps`（默认 `60`），像素格式 `yuv420p`。
   - 音频统一为双声道 48000Hz AAC 128kbps；静态图片注入 `anullsrc=r=48000:cl=stereo` 静音轨。
2. 动态画布计算：
   - `--canvas-mode max`（默认）：取分片内所有素材尺寸在不超过 `--max-resolution`（默认 8K `7680x4320`）下的**最大包围盒**，保证 4K 素材维持 4K，不产生画质降采样。
   - 尺寸强制规范化为偶数（H.264 规范要求）。
   - 滤镜通过 `scale=...:force_original_aspect_ratio=decrease,pad=...:(ow-iw)/2:(oh-ih)/2,setsar=1` 自动补黑边居中。
3. HEIC/HEIF 兼容性：
   - 在转码前通过 Pillow 将 HEIF 图像转为中间 PNG，并调用 `ImageOps.exif_transpose` 纠正旋转方向。
4. 图片 Ken Burns 效果：
   - `--ken-burns` 启用时使用 `zoompan` 滤镜缓慢推拉缩放，关闭时保持静态帧以节省码率与提升画质。
5. 无损合并：
   - 所有素材先分别转为参数一致的临时 MP4 片段，最后通过 `ffmpeg -f concat -c copy` 极速无损合并。
6. 章节计算：
   - 纯函数 `build_chapters`：每文件一章，首章为 `0:00`，相邻素材间隔 < 10 秒自动合并；格式化为 YouTube 简介接受的章节格式。

**验收标准**：
- 章节合并阈值、首章 0:00 及累计时长精确对应。
- 转码输出符合 H.264 / AAC 48kHz / yuv420p 标准。
- HEIF 图像旋转正确无黑屏或解码崩溃。
- Concat 流程成功产出合并视频。

---

## M5：OAuth 登录 + 上传 + 播放列表 + 配额

**目标**：YouTube Data API v3 授权与断点上传，支持内置登录、播放列表维护与 403 配额退避感知。

**新增依赖**：google-api-python-client、google-auth-oauthlib。

**交付**：`tubetape/auth.py`、`tubetape/uploader.py`、`tests/test_auth.py`、`tests/test_uploader.py`。

**任务清单**：
1. 权限 Scope：
   - 统一采用 `https://www.googleapis.com/auth/youtube.force-ssl`（具备上传、删除旧片、播放列表读写及元数据修改权限）。
   - 设置 `OAUTHLIB_RELAX_TOKEN_SCOPE=1`，防止 Google 回传 scope 差异引起异常。
2. 内置 OAuth 流程（`tubetape --login`）：
   - `auth.headless_oauth_flow`：适用于无浏览器服务器环境。打印授权 URL，等待用户粘贴重定向 URL（含 code），同一 Flow 实例内部完成 PKCE 校验兑换。
   - 生成的 `token.json` 设置 `0600` 文件权限（仅所有者可读写）。
   - 支持环境变量 `TUBETAPE_TOKEN` 注入凭据 JSON 字符串。
3. 可断点重试上传：
   - 使用 `MediaFileUpload(..., chunksize=-1, resumable=True)` 执行分块上传。
   - 指数退避重试网络瞬断（`_execute_with_retry`）。
4. 403 配额耗尽检测：
   - 解析 HttpError 状态码与响应体，当检测到 403 且包含 `quota` 时，抛出特定的 `QuotaExceededError`。
5. 播放列表：
   - 支持 `--playlist <PLAYLIST_ID>`，上传成功后按拍摄时间顺序追加。

**验收标准**：
- `test_auth.py` 覆盖 PKCE flow 构造、URL 解析与权限校验。
- `test_uploader.py` 覆盖 403 配额异常捕获、指数退避重试与元数据组装。
- mock 环境下不发生真实网络交互。

---

## M6：重建 + 动态监控 + flush + 优雅停机

**目标**：实现不可变分片的原子重建（先传新片再删旧片），基于 watchdog 和 mtime 轮询的双层文件监控，支持静默期去抖与退出时安全 flush。

**新增依赖**：watchdog。

**交付**：`tubetape/rebuild.py`、`tubetape/watcher.py`、`tests/test_rebuild.py`、`tests/test_watcher.py`。

**任务清单**：
1. 原子重建状态机（`Rebuilder`）：
   - 步骤：1. 转码新分片 → 2. 上传新视频 → 3. 校验时长与章节 → 4. 删除旧视频（仅当新片校验通过后） → 5. 提交新分片至数据库，旧 `youtube_video_id` 追加至 `previous_video_ids`。
   - 任何前置步骤失败，旧视频保持不变。
2. 双层文件监控体系（`run_watch`）：
   - **底层事件**：watchdog `Observer` 递归监控创建与移动事件，过滤媒体扩展名并忽略隐藏文件。
   - **兜底轮询**：`MtimeScanner` 仅比对目录修改时间与子目录列表，以 $O(\text{目录数})$ 开销为网络挂载盘（如 NFS/SMB）提供事件漏报兜底。
3. 静默期去抖与配额恢复：
   - 检测到文件变更后等待 `--quiet-period`（默认 10 分钟）静默期，防止文件拷贝未完成即开始处理。
   - 触发配额耗尽后挂起退避 `--quota-backoff`（默认 1 小时），到期后自动重新尝试上传，不依赖本地配额计数。
4. 优雅退出（Graceful Shutdown）：
   - 捕获 `SIGINT` 与 `SIGTERM`（Docker 停机信号），将其转换为 `KeyboardInterrupt`，触发设置 `args.flush = True` 执行最后一次管道封片并保存数据库后安全退出。

**验收标准**：
- 重建失败时旧视频不被删除，数据库记录保持正确。
- `MtimeScanner` 在平稳期无变更，新文件写入子目录时能准确捕获。
- 优雅停机信号能触发 flush 流程。
- `test_rebuild.py`、`test_watcher.py` 全绿。

---

## M7：进度展示 + 集成验证 + 统一日志

**目标**：构建兼顾终端交互体验与非交互重定向的进度展示系统，提供详细的 `<db>.log` 审计追踪，串联全流程与退出码。

**新增依赖**：rich。

**交付**：`tubetape/ui.py`、`tubetape/log.py`、`tubetape/cli.py`、`tests/test_ui.py`。

**任务清单**：
1. 统一日志体系（`log.py`）：
   - 控制台 stderr：默认仅展示 WARNING+，加 `-v` 提升至 INFO，`-vv` 提升至 DEBUG。
   - 独立审计日志文件：默认在数据库同级输出 `<db>.log`（如 `tubetape.json.log`），全量记录带纳秒级时间戳的 DEBUG 审计信息（包含哈希详情、ffprobe/ffmpeg 完整参数、上传重试等）。
   - 抑制 `googleapiclient`、`urllib3`、`watchdog` 等第三方库的冗长内部日志。
2. 进度上报器（`ui.py`）：
   - TTY 交互终端：使用 `rich.console` 展示实时任务状态与动态进度。
   - 非 TTY / 重定向环境：自动回退为简洁的单行文本输出，便于 CI 或后台 nohup 运行。
3. 管道执行与资源清理：
   - `cli.py` 串联：`scan -> plan -> (dry-run 预览中断) -> transcode -> upload -> watch`。
   - 本地转码生成的隐藏临时 `.mp4` 文件在上传成功后立即删除，避免撑爆本地磁盘空间。
4. 退出码明确：
   - 成功退出返回 `0`；关键参数缺失或致命错误返回 `1`；配额耗尽返回 `2`。

**验收标准**：
- 非交互模式下输出干净无乱码。
- 本地转码临时文件上传后被正常清理。
- `--dry-run` 完整预览分片且对数据库零写入。
- `test_ui.py` 通过。

---

## M8：文件名时间兜底

**目标**：针对被剥离 EXIF 元数据（如微信/QQ 传输）但文件名中编码日期的媒体文件，通过文件名时间替代文件修改时间（mtime）。

**交付**：`tubetape/scanner.py`（`parse_filename_time` 与 `ScannedFile.time_source`）。

**任务清单**：
1. 识别典型时间命名正则：
   - `(\d{4})_(\d{2})_(\d{2})_(\d{2})_(\d{2})(?:_(\d{2}))?`（微信/QQ 格式 `2020_12_16_01_00_...`）。
   - `(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})`（手机相机格式 `20201216_010000`）。
   - `(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(?!\d)`（短格式 `20201216_0100`）。
   - `(\d{4})-(\d{2})-(\d{2})[ _](\d{2})-(\d{2})-(\d{2})`（连字符格式）。
2. 合法性校验：月（1~12）、日（1~31）、时（0~23）、分/秒（0~59），非法数值自动忽略。
3. 优先级策略：插入在 EXIF 之后与 mtime 兜底之前，按 `--timezone` 解释后转换为 UTC ISO 8601。
4. 记录打标：`time_source = "filename"`，`missing_meta = true`。

**验收标准**：
- 无 EXIF 但有时间文件名的文件被标记为 `filename` 且提取出准确时间。
- 包含 EXIF 的文件优先采用 EXIF，不被文件名覆盖。
- 非法时间格式平滑降级至 mtime。

---

## M9：标题内嵌对账、纯时间戳简介与云端容灾

**目标**：彻底解决 YouTube API 截断简介导致对账 Marker 丢失以及本地库损坏引发重复上传的问题，实现完全基于云端无损标题的对账机制与文件级点击跳转。

**新增依赖**：无（基于 YouTube Data API v3 与 Python re）。

**交付**：`tubetape/reconcile.py`、`tubetape/planner.py`、`tests/test_reconcile.py`。

**任务清单**：
1. 标题内嵌短分片 ID：
   - 标题规则修改为 `{首文件时间戳} - {末文件时间戳} [{short_id}]`（如 `20240101-120000 - 20240101-122000 [a1b2c3d4e5f67890]`）。
   - YouTube `playlistItems.list(part="snippet")` 永远不会截断 `snippet.title`（~52 字符，远低于 YouTube 100 字符限制）。
2. 纯文件时间戳简介：
   - 移除尾部容易被截断的文本 Marker，简介改为每个文件对应的时间戳列表（`0:00 20240101-120000\n0:03 20240101-120003`）。
   - `build_chapters(min_gap=0.0)` 为每个媒体文件独立生成起始时间点，YouTube 播放器自动将其识别为可点击跳转锚点。
   - `chapters_text` 限制输出字符在 4800 字符以内，防止超出 YouTube 5000 字符上限。
3. 双向容灾对账：
   - `extract_segment_id_from_title(title)` 正则匹配标题中的 `[16位hex]` 与 `-16位hex`。
   - `fetch_remote_index(service)` 优先提取标题中的 ID，同时保留旧版简介 Marker 作为兼容兜底，在索引中双向登记 64 位全哈希与 16 位前缀。
   - 管道在转码前检查远程索引，已上传分片直接登记本地库为 `sealed` 并跳过转码上传；重建分片若云端已存在新片则清理旧记录，保障绝对幂等。

**验收标准**：
- 覆盖标题短 ID 提取、多页 uploads 遍历与标题/简介双重回落测试。
- 视频简介干净清爽，所有素材均可点击跳转。

---

## M10：容器化、跨平台打包与运维发布

**目标**：提供生产级 Docker 镜像、docker-compose 编排、PyInstaller 独立二进制构建方案，以及符合 Google OAuth 审核要求的文档站点。

**新增依赖**：PyInstaller（仅打包工具）。

**交付**：`Dockerfile`、`docker-compose.yml`、`tubetape.spec`、`scripts/build.py`、`scripts/docker_build.sh`、`docs/`。

**任务清单**：
1. Docker 容器化：
   - 基础镜像 `python:3.11-slim`，安装 `ffmpeg` 与 `fontconfig`。
   - 创建非 root 用户 `tubetape:1000`，挂载点声明 `/data`（媒体输入）与 `/db`（数据库与凭据）。
   - 映射端口 `8080:8080` 用于实时 Web 控制台。
   - 默认入口 `tubetape`，支持 SIGTERM 优雅退出。
2. `docker-compose.yml` 编排：
   - 声明 `MEDIA_DIR`、时区 `TZ` 与端口 `8080:8080`。
   - 挂载 `./` 到容器内 `/db`，统一存储 `tubetape.json`、日志、`client_secret.json` 与 `token.json`。
   - 支持 `docker compose run --rm tubetape --login` 一键交互登录。
3. PyInstaller 跨平台打包：
   - `tubetape.spec` 与 `scripts/build.py`：打包为单个无外部 Python 依赖的可执行文件，检查系统 ffmpeg/ffprobe 环境。
4. Google OAuth 发布文档与条款：
   - `docs/` 部署至 GitHub Pages，包含首页、中英双语《服务条款》（`Terms_of_Service.md`）与《隐私政策》（`Privacy_Policy.md`），满足 GCP 生产环境应用发布审核规范。

**验收标准**：
- `docker build` 镜像构建顺利，容器内能正常执行 `--dry-run` 与 `--login`。
- `docker compose` 一键启动与停止平稳工作。

---

## M11：Web 实时控制台、OAuth 流程自愈与分块断点续传

**目标**：实现无多余依赖的实时 Web 日志控制台、自愈型 OAuth 凭据流、Resumable 分块断点续传与安全退出机制，彻底根除多线程/重启导致的重复上传。

**新增依赖**：无（基于 Python 标准库 `http.server`、`threading`、`urllib`、`google-auth`）。

**交付**：`tubetape/web.py`、`tubetape/uploader.py`、`tubetape/auth.py`、`tests/test_web.py`。

**任务清单**：
1. Web 实时控制台与日志查看（`tubetape/web.py`）：
   - 内置 `ThreadingHTTPServer` 运行于后台 Daemon 线程，不阻碍主管道退出。
   - 提供现代化暗色主题前端：显示运行状态 Badge、任务描述、带色彩高亮（INFO/WARN/ERROR）的实时日志终端、自动滚动与清屏按钮。
   - 接口设计：
     - `GET /`：控制台 HTML 界面。
     - `GET /api/status`：返回当前运行状态与步骤。
     - `GET /api/logs?offset=<int>&initial=<0|1>`：日志文件增量拉取，初次加载仅保留末尾 64KB 避免浏览器卡顿。
     - `GET /?code=...`：自动拦截 OAuth 回调并更新授权码队列。
   - CLI 支持 `--web-port 8080`（0 为禁用），Reporter 状态实时同步至 Web 界面。
2. OAuth 自动化与凭据自愈（`tubetape/auth.py`）：
   - `ensure_credentials` 函数统一调度：
     1. 优先检查 `TUBETAPE_TOKEN` 环境变量。
     2. 读取 `token.json`，若过期且含有 refresh token，自动调用 Google API 完成 refresh 并将最新凭据写回磁盘（权限 `0600`）。
     3. 若 `token.json` 不存在或无法刷新，检查 `client_secret.json` 是否存在。交互环境（TTY）自动触发 headless 授权提示并捕获跳转；非交互环境（如 Docker 守护进程）抛出包含 `docker compose run --rm tubetape --login` 的明确操作指引。
3. 真实分块断点续传（`tubetape/uploader.py`）：
   - 上传改为 10MB 分块大小（`chunksize=10*1024*1024`）。
   - 通过 `request.next_chunk()` 循环推进上传，网络超时或断开时在当前 offset 处按指数退避重试，不再重新发起整个视频的上传创建请求。
   - 播放列表加入操作（`add_to_playlist`）增加异常保护，即使加入列表失败也不阻断主分片写入数据库。
4. 规划器边界时间碰撞消除与平稳停机：
   - `tubetape/planner.py` 建立 `existing_owner` 映射，已归属分片的文件不再参与相邻同秒照片的范围争抢，消除规划器震荡。
   - `tubetape/cli.py` 在 `run_watch` 中捕获 SIGTERM/SIGINT 时直接优雅停止监听器并退出，不再触发耗时的 `flush=True` 强制转码，避免 Docker 10 秒超时强制 SIGKILL 产生孤儿视频。

**验收标准**：
- `test_web.py` 覆盖控制台各个 API 端点与 OAuth 拦截。
- `test_auth.py` 覆盖凭据有效性校验、自动 refresh 写入以及交互/非交互分支。
- 全部 234 项单元测试 100% 通过。

---

## M12：方案 A 一体化 Web OAuth 授权流

**目标**：彻底解决非交互式环境（如 `docker compose up -d`）在缺少 `token.json` 时崩溃退出的痛点，实现开箱即用的**一体化 Web OAuth 授权流（Scheme A）**：Web 控制台常驻在线，浏览器一键授权或粘贴重定向 URL 提交，凭据自动落盘后后台无缝平稳起跑，零重启要求。

**新增依赖**：无。

**交付**：`tubetape/auth.py`、`tubetape/web.py`、`tubetape/cli.py`、`tests/test_auth.py`、`tests/test_web.py`。

**任务清单**：
1. `OAuthSession` 会话封装（`tubetape/auth.py`）：
   - 将 `InstalledAppFlow` 及其 PKCE 校验状态完整封装在 `OAuthSession` 对象中，确保授权 URL 生成与 Token 换取在同一个 Flow 实例内执行，消除 PKCE `code_verifier` 不匹配问题。
   - `session.exchange(code_or_url)`：统一支持纯 `code` 或浏览器地址栏完整 URL（自动解析 `?code=...` 并解码 URL 字符），完成后以 `0600` 权限将凭据持久化至 `token.json`。
2. Web 控制台状态感知与提交端点（`tubetape/web.py`）：
   - 维护线程安全状态：`oauth_session`、`auth_event`、`auth_error`。
   - `GET /api/status`：动态返回 `auth_required: bool` 与 `auth_url: str | None`。
   - `POST /api/auth/submit` 与 `GET /api/auth/submit`：支持手动提交重定向 URL 或 code 换取 Token，换取成功唤醒等待事件。
   - `GET /?code=...`：本地直接回调拦截，自动兑换并展示 3 秒自动返回控制台的成功页。
   - Web 控制台前端嵌入现代化暗色卡片 `#auth-banner`：
     - **方式一（一键直接授权）**：点击直达 Google 登录页面，本机自动回调完成。
     - **方式二（手动粘贴地址栏 URL）**：面向远程 NAS 用户，若浏览器跳转 `localhost:8080` 报错，直接复制粘贴完整地址栏 URL 提交即可。
   - `web.wait_for_auth(timeout=3600.0)`：以 1 秒间隔在 `threading.Event` 上响应式等待，对 SIGTERM/SIGINT 即时退出，不发生挂死。
3. 管道早启动与无感起跑（`tubetape/cli.py`）：
   - `main()` 启动 Web 控制台后进入管道，非 dry-run 模式下前置校验/等待凭据，避免海量扫描后再中断。
   - Web 授权完成后，自动返回凭据并无缝继续扫描与上传，**完全无需手动重启 Docker 容器**。
   - 保留 `--login` 作为纯命令行与 SSH 运维的交互式备用工具。

**验收标准**：
- `test_auth.py` 覆盖 `OAuthSession` 纯 code 与完整 URL 换取、异常输入处理及 Web 授权等待。
- `test_web.py` 覆盖 `/api/auth/submit` POST 换取、GET 回调兑换、状态同步与超时处理。
- 全部 243 项单元测试 100% 通过。

---

## 附：YouTube 凭据准备与操作指南

### 1. Google Cloud Console 一次性配置

1. **新建项目**：访问 [console.cloud.google.com](https://console.cloud.google.com) 创建项目。
2. **启用 API**：搜索并启用 **YouTube Data API v3**。
3. **创建 OAuth 客户端**：凭据 → 创建凭据 → **OAuth 客户端 ID → 桌面应用**（Desktop App），下载 `client_secret.json`。
4. **OAuth 同意屏幕**：
   - 用户类型选 **External**。
   - 添加测试用户（自己的 Google 账号邮箱）。
   - 添加权限范围 Scope：`https://www.googleapis.com/auth/youtube.force-ssl`。
5. **发布档位**：
   - **Testing（免审核）**：可立即使用；refresh token 约 7 天过期，过期后重新执行登录命令。
   - **In production（长期有效）**：点击 "Publish app"，凭据长期有效；个人自用无需提交第三方陌生用户审核。

### 2. 授权登录（生成 token.json）

TubeTape 已内置无浏览器环境的 OAuth 流程：

```bash
# 本地 Python 环境
python -m tubetape --login --client-secret client_secret.json

# Docker 环境
docker compose run --rm tubetape --login
```

1. 终端打印 Google 授权 URL。
2. 浏览器打开链接完成账号授权。
3. 授权后浏览器会跳转至形如 `http://localhost:8080/?state=...&code=...` 的地址（页面打不开无需理会）。
4. 复制浏览器地址栏的完整 URL 并粘贴回终端。
5. 程序自动兑换并在数据库同级目录生成权限为 `0600` 的 `token.json`。

### 3. 日常运行推荐命令

```bash
# 1. 预览检查（Dry-Run）
python -m tubetape --input /path/to/media --dry-run

# 2. 启动持续监听（长期挂机）
python -m tubetape --input /path/to/media --watch

# 3. 仅备份真实相机与手机素材（过滤截图/转存网络视频）
python -m tubetape --input /path/to/media --only-camera-photos --only-phone-videos --watch

# 4. 实时查看全量详细审计日志
tail -f /path/to/media/tubetape.json.log
```
