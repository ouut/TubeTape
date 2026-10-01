# TubeTape 开发里程碑

> 本文档是 `prompt.md` 的**可执行拆分**。每个里程碑是一轮 `/review-loop` 的独立目标。
> 每轮流程：worker 实现 → reviewer 审查（正确性 + 测试覆盖）→ worker 按意见修复。
> **里程碑按顺序推进，前一个验收通过再进入下一个。**

## 总览

| 里程碑 | 主题 | 关键交付 | 新增依赖 |
|---|---|---|---|
| M0 | 项目骨架 + CLI 参数解析 | `cli.py`、`durations.py`、`test_cli.py` | pytest |
| M1 | 数据库 schema + 原子读写 | `db.py`、`test_db.py` | — |
| M2 | 扫描与建库 | `scanner.py`、`test_scanner.py` | Pillow、exifread |
| M3 | 分片规划 | `planner.py`、`test_planner.py` | — |
| M4 | 转码 + 章节计算 | `transcoder.py`、`chapters.py` | ffmpeg（系统） |
| M5 | OAuth 登录 + 上传 + 播放列表 + 配额 | `auth.py`、`uploader.py` | google-api-python-client、google-auth-oauthlib |
| M6 | 重建 + 动态监控 + flush | `rebuild.py`、`watcher.py` | watchdog |
| M7 | 进度展示 + 集成验证 | `ui.py`、全流程串联 | rich |
| M8 | 文件名时间兜底 | `scanner.py`（`parse_filename_time`） | — |

## 目录结构

```
TubeTape/
├── prompt.md            # 需求规格（只读，实现依据）
├── development.md       # 本文件：里程碑拆分
├── pyproject.toml
├── tubetape/
│   ├── __init__.py
│   ├── cli.py           # 入口 + 参数解析（每个里程碑都维护）
│   ├── durations.py     # 时长/分辨率/时区解析（M0）
│   ├── db.py            # 数据库 + 原子读写（M1）
│   ├── scanner.py       # 扫描 + 元数据 + file_id（M2）
│   ├── planner.py       # 分片规划（M3）
│   ├── transcoder.py    # 转码（M4）
│   ├── chapters.py      # 章节（M4）
│   ├── auth.py          # OAuth（M5）
│   ├── uploader.py      # 上传 + 播放列表 + 配额（M5）
│   ├── rebuild.py       # 重建（M6）
│   ├── watcher.py       # 监控 + flush（M6）
│   └── ui.py            # rich 进度（M7）
└── tests/
    ├── test_cli.py      # 始终维护
    ├── test_durations.py
    ├── test_db.py
    ├── test_scanner.py
    ├── test_planner.py
    ├── test_transcoder.py
    ├── test_chapters.py
    ├── test_uploader.py
    ├── test_rebuild.py
    └── test_watcher.py
```

## 贯穿性约定（每个里程碑都必须遵守，违反即 reviewer P0）

1. **时间戳**：内部/排序统一 UTC，用 epoch 秒排序，存储 ISO 8601 `YYYY-MM-DDTHH:MM:SSZ`；展示与文件名用 `YYYYMMDD-HHMMSS`（UTC）。拍摄时间来源优先级（M8 扩展后）：视频 `creation_time` → EXIF `DateTimeOriginal` → **文件名时间模式** → mtime 兜底；文件记录用 `time_source` 字段标注 `metadata | filename | mtime`。
2. **file_id**：`sha256(文件内容)`；> 512 MB 用「前 4 MB + 中 4 MB + 后 4 MB + 文件大小」快速哈希，阈值可配置。路径/文件名变化不影响 ID。
3. **segment_id**：`sha256(排序后的 file_id 列表 + 分片参数)`。内容变 → ID 变 → 才需要重建/上传。
4. **分片文件名/标题**：`{首文件时间戳} - {末文件时间戳}`，如 `20240101-153000 - 20240101-160000`。
5. **数据库原子写**：先写同目录临时文件再 `os.replace`（rename），防止崩溃损坏；每次状态变更立即落盘。
6. **核心设计决策不可违背**：紧凑拼接（不补时间空洞）；已上传分片不可原地修改（先传新片成功后再删旧片）；已有分片时间区间固定（不级联重建）；重建顺序「转码上传新片 → 校验 → 删旧片 → 更新库」。
7. **测试数据**：单测用 `tests/fixtures/` 下的小样本（几张图 + 1-2 秒合成视频）；不要对 `/workspace/u`（33 GB 真实数据）跑全量转码/上传。端到端验证只做 `--dry-run`。

---

## M0：项目骨架 + CLI 参数解析

**目标**：搭好可运行、可测试的骨架，完整解析 `prompt.md` 启动参数表的所有参数与默认值。

**新增依赖**：pytest（dev）。

**交付**：`pyproject.toml`、`tubetape/__init__.py`、`tubetape/cli.py`、`tubetape/durations.py`、`tests/test_cli.py`、`tests/test_durations.py`。

**任务清单**：
1. `pyproject.toml`：`requires-python >= 3.11`，pytest 作为 dev 依赖，`[project.scripts] tubetape = "tubetape.cli:main"`。
2. argparse 定义全部 14 个参数，默认值精确匹配下表：

   | 参数 | 默认值 |
   |---|---|
   | `--image-duration` | `3` |
   | `--input` | 当前目录 |
   | `--db` | `<input>/tubetape.json`（运行时拼接，不是 argparse default） |
   | `--segment-duration` | `20m` |
   | `--timezone` | 系统本地时区 |
   | `--crf` | `18` |
   | `--max-resolution` | `3840x2160` |
   | `--ken-burns` | 关 |
   | `--privacy` | `private`（choices: private/unlisted） |
   | `--no-rebuild` | 关 |
   | `--rebuild-cooldown` | `24h` |
   | `--flush` | 无 |
   | `--watch` / `--no-watch` | 默认开（BooleanOptionalAction） |
   | `--dry-run` | 无 |

3. `durations.py`：
   - `parse_duration("20m" | "1200s" | "0:20:00" | "1200") -> float 秒`，非法输入抛清晰错误。
   - `parse_resolution("3840x2160") -> (w, h)`，非法报错。
   - `parse_timezone("Asia/Shanghai") -> zoneinfo.ZoneInfo`，非法报错；缺省取系统本地时区。
4. `cli.py` 暴露 `parse_args(argv=None) -> Namespace`（便于测试），并做 `--db` 默认值拼接、`--input` 转绝对路径等后处理。
5. `main()` 先只做「解析 → 打印解析结果」，不接后续逻辑（M7 再串联）。

**验收标准**：
- `test_cli.py` 覆盖每个参数：默认值、显式传值、非法值报错（如 `--privacy public`、`--crf abc`）。
- `test_durations.py` 覆盖三种时长格式、纯数字、边界（`0`、负数、超大）、`0:00:10`、分辨率、时区。
- `pytest` 全绿。

---

## M1：数据库 schema + 原子读写

**目标**：实现 `prompt.md` 的 JSON 数据库 schema，原子写入、幂等加载。

**新增依赖**：无（标准库）。

**交付**：`tubetape/db.py`、`tests/test_db.py`。

**任务清单**：
1. `Database` 类：`load(path)` / `save()` / 字段访问器（files/segments/queue/errors/settings），`version: 1`。
2. 严格按 `prompt.md` 的 schema：
   - file：`path / name / type / captured_at_utc / resolution / duration_seconds / size_bytes / sha256 / location / missing_meta`
   - segment：`file_ids / range / duration_seconds / output_path / youtube_video_id / previous_video_ids / status / chapters / last_rebuilt_at / attempts / error`
   - `queue.pending_file_ids`、`queue.rebuild_segment_ids`；`errors` 列表；`settings`。
3. 原子写：写同目录临时文件再 `os.replace`，失败不损坏原文件。
4. 损坏 JSON（解析失败）→ 明确报错并保留备份，不静默覆盖。
5. 辅助方法：按 `file_id`/`segment_id` 查询与更新、追加 errors、状态转移。
6. 提供 `missing_meta`、`previous_video_ids`、`status` 枚举常量（`pending | encoding | uploading | sealed | rebuild_pending | failed | skipped`）。

**验收标准**：
- 新建 / 加载 / 保存往返数据一致。
- 原子替换（模拟写入中断不损坏原文件）。
- 损坏 JSON 处理。
- 所有 schema 字段读写正确。

---

## M2：扫描与建库

**目标**：递归扫描 `--input`，提取元数据与 file_id，与库对比识别新增/已处理/已删除，损坏文件跳过并记录。

**新增依赖**：Pillow、exifread；ffmpeg（系统命令解析视频元数据）。

**交付**：`tubetape/scanner.py`、`tests/test_scanner.py`、`tests/fixtures/`（小样本）。

**任务清单**：
1. 递归扫描图片/视频扩展名（jpg/jpeg/png/heic/tif + mp4/mov/m4v 等，扩展名列表可配置）。
2. 提取：名称、路径（相对 `--input`）、类型、分辨率、视频时长、文件大小、拍摄时间、GPS（可选，仅入库）。
3. 拍摄时间优先级：
   1. 视频：QuickTime/MOV `creation_time` 等元数据（优先 UTC）；
   2. 照片：EXIF `DateTimeOriginal`（无时区，按 `--timezone` 解释）；
   3. 兜底：文件 mtime，标记 `missing_meta: true`。
4. 时间统一转 UTC：内部 epoch 秒排序，存储 ISO 8601 `YYYY-MM-DDTHH:MM:SSZ`。
5. `file_id = sha256(内容)`；> 512 MB 用「前 4 MB + 中 4 MB + 后 4 MB + 大小」快速哈希（阈值可配置）。
6. 与数据库对比：新增 / 已处理 / 已删除三类。
7. 损坏/无法解析 → 跳过并写入 `errors`，不中断整体流程。
8. 视频元数据用子进程 `ffmpeg -i` 解析（注意从其 stderr 取信息），解析失败按兜底处理。
9. 扫描返回结构化结果供 planner 使用；`--dry-run` 同样完整扫描。

**验收标准**：
- 图片 EXIF 时间、视频 `creation_time`、mtime 兜底、`--timezone` 解释。
- file_id 稳定性（改路径/文件名不变）、快速哈希与大文件阈值。
- 损坏文件跳过 + errors 记录、不中断。
- 新增/已处理/已删除识别正确。

---

## M3：分片规划

**目标**：按拍摄时间紧凑拼接生成分片计划；已有分片区间固定；计算 segment_id；跳过已存在分片。

**新增依赖**：无。

**交付**：`tubetape/planner.py`、`tests/test_planner.py`。

**任务清单**：
1. 按拍摄时间升序排序。
2. 首次运行：贪心打包，累计内容时长 ≤ `--segment-duration`。
3. 后续运行：已有分片 `[start_ts, end_ts)` 视为**固定边界**（区间互不重叠且递增）；新文件落入哪个区间归哪个分片；区间之外的文件按贪心打包成新分片。
4. 单个视频时长 > `--segment-duration` → **不切割**，独立成片（可超配置值）。
5. 图片按 `--image-duration` 计时长；视频按实际时长。
6. `segment_id = sha256(排序后的 file_id 列表 + 分片参数)`。
7. 已存在的 `segment_id` → 跳过（不转码不上传）。
8. flush 封片：实现纯函数「给定文件列表 + flush 标志 → 不足时长也封片」（触发时机 M6 做）。
9. `range` 存 ISO 8601 UTC `[start, end)`。
10. 文件名/标题 = `{首时间戳} - {末时间戳}`（`YYYYMMDD-HHMMSS`）。

**验收标准**：
- 贪心打包边界（累计恰好 = 时长、超过时长、单视频超大独立成片）。
- 固定区间不重排（加一张照片只影响它所在分片，不级联）。
- 区间外文件打包成新分片。
- segment_id 稳定性与变化（增删文件 / 改参数 → 变；不变则跳过）。
- flush 封片、range 计算、标题格式。

---

## M4：转码 + 章节计算

**目标**：用 ffmpeg 生成 MP4，转码规格严格按 `prompt.md`；同时计算章节数据。

**新增依赖**：ffmpeg（系统）。

**交付**：`tubetape/transcoder.py`、`tubetape/chapters.py`、`tests/test_transcoder.py`、`tests/test_chapters.py`。

**任务清单**：
1. 容器/编码：MP4、H.264（High profile，yuv420p）、音频 AAC。
2. CRF 默认 `18`；不放大，目标分辨率 = `min(原分辨率, --max-resolution)`。
3. 帧率与原视频一致。
4. 图片：默认静态帧；`--ken-burns` 缩放平移；EXIF 方向自动纠正；横竖混排 letterbox 补齐到分片统一分辨率。
5. 所有片段先转成参数一致的中间片段再 concat，避免二次画质损失；图片段插入静音音轨。
6. 音频统一 48 kHz 立体声 AAC（保证可无损 concat）。
7. 输出到临时目录；转码前检查磁盘剩余空间。
8. 章节：每文件一章，标题 = 时间戳（可选附地点/文件名）；第一章 `0:00`；相邻间隔 < 10 秒合并为一章；时间戳必须与实际时长精确对应。
9. 章节文本格式（写入简介）：
   ```
   0:00 20240101-153000
   0:03 20240101-153100
   ```

**验收标准**：
- `test_chapters.py`（纯函数）：< 10s 合并、第一章 `0:00`、标题格式、10s 整的边界、与时长精确对应。
- `test_transcoder.py`（冒烟）：用 ffmpeg 合成 1-2 秒测试视频/图片，ffprobe 验证 h264/yuv420p/aac/48kHz、图片静态帧时长、letterbox、concat 结果时长正确。

---

## M5：OAuth 登录 + 上传 + 播放列表 + 配额

**目标**：YouTube API 上传，元数据完整，配额感知重试，加入按拍摄时间排序的播放列表。

**新增依赖**：google-api-python-client、google-auth-oauthlib。

**交付**：`tubetape/auth.py`、`tubetape/uploader.py`、`tests/test_uploader.py`。

**任务清单**：
1. auth：`client_secret.json` + `token.json`（或环境变量 token 字符串）；scope `youtube.upload` + `youtube.readonly`；refresh token 长期有效需 OAuth 客户端 "In production"；token 文件权限仅当前用户可读。
2. uploader：`videos.insert`；title = `{首} - {末}`；description 含章节时间戳；`privacyStatus`（private/unlisted）、`madeForKids=false`、`categoryId`、`selfCertification` 显式设置。
3. 上传成功后校验章节/时长 → 加入按拍摄时间排序的播放列表 → 标记 `sealed` → 清理临时文件。
4. 配额：单次上传 1600 units，默认 10000/天；显示剩余配额；超出排队到次日（默认）或提示用户。
5. 失败自动重试：指数退避、配额感知；重试次数与状态写库。
6. 上传进度回调参数（供 M7 的 rich 使用，这里先留 hook）。

**验收标准**（mock API，不真上传）：
- title/description/隐私/selfCertification/章节文本正确。
- 播放列表插入按拍摄时间排序。
- sealed 状态转移、临时文件清理。
- 配额计算与超限排队。
- 指数退避重试逻辑。

---

## M6：重建 + 动态监控 + flush 触发

**目标**：不可变分片的「先传后删」重建、去抖、配额感知；watchdog 监控 + flush 触发。

**新增依赖**：watchdog。

**交付**：`tubetape/rebuild.py`、`tubetape/watcher.py`、`tests/test_rebuild.py`、`tests/test_watcher.py`。

**任务清单**：
1. 重建顺序（严格）：先转码上传新片 → 校验章节/时长通过 → 删除旧片 → 更新数据库（旧 `video_id` 保留到删除成功为止）。任何一步失败都保留旧片。
2. 去抖：同一分片在冷却期（默认 `24h`）内多次变更合并为一次重建；重建前等待静默期（默认 `10min`）。
3. 配额感知：重建同样 1600 units/次，纳入预算，超出排队到次日。
4. 新文件落入已封口分片区间内 → 默认标记 `rebuild_pending`；`--no-rebuild` 时改为进入补录队列（单独成片，不动旧片）。
5. watcher：watchdog 监控新增文件 + 轮询兜底；区间外 → 入队攒够时长；区间内 → 按上一条。
6. flush 触发（任一）：`--flush`；队列空闲超过可配置时间（默认 `1h`）；收到退出信号（正常退出封片后退出）。
7. 状态机 `pending | encoding | uploading | sealed | rebuild_pending | failed | skipped` 转移正确。

**验收标准**：
- 成功重建（新传 → 删旧 → 更新库）、失败路径（上传失败 → 旧片保留）。
- 去抖合并、静默期、配额超限排队。
- `--no-rebuild` 补录。
- 新增文件落入区间内/外两个分支。
- flush 三种触发（mock 时间/信号）。

---

## M7：进度展示 + 集成验证

**目标**：rich 动态界面 + 非交互日志 + 端到端 dry-run 验证 + 断点续传。

**新增依赖**：rich。

**交付**：`tubetape/ui.py`、`cli.py` 串联全流程、`tests/test_cli.py` 补充冒烟测试。

**任务清单**：
1. rich 展示：当前任务（扫描/转码/上传）+ 文件、转码/上传进度条、当日配额用量、队列与待重建状态。
2. 非交互模式（无 TTY）输出简洁日志，便于重定向。
3. `cli.py` 串联：scan → plan → transcode → upload → watch；`--dry-run` 只做 scan + plan + ID 计算。
4. 断点续传：从数据库状态恢复，已封口且未变更的分片不重复处理。
5. 崩溃恢复：下次启动从数据库状态继续。

**验收标准**：
- 无 TTY 下 `--dry-run` 对 `/workspace/u` 的小样本完整跑通（不转码不上传），输出分片计划 + file_id/segment_id。
- 非交互日志重定向正常。
- 此前所有测试仍全绿。

---

## M8：文件名时间兜底

**目标**：EXIF 被剥离（微信/QQ 压缩）但文件名编码日期的照片，用文件名时间代替 mtime。

**背景**：`/workspace/u` 里大量照片无 `DateTimeOriginal`，只有 `2020_12_16_01_00_IMG_6558.JPG` 这类命名；只用 mtime 会把归档时间全部错成拷贝时间。

**交付**：`scanner.py` 的 `parse_filename_time(name, tz)` + `ScannedFile.time_source` 字段。

**任务清单**：
1. 识别常见文件名时间模式：`YYYY_MM_DD_HH_MM[_SS]_*`（微信/QQ）、`IMG_/VID_YYYYMMDD_HHMMSS`、`YYYYMMDD_HHMM`、`YYYY-MM-DD HH-MM-SS`。
2. 日期范围校验（月 1-12、日 1-31、时 0-23、分/秒 0-59），越界当作无时间。
3. 优先级插入 EXIF 与 mtime 之间；文件名时间按 `--timezone` 解释后转 UTC。
4. `time_source` 标注 `metadata | filename | mtime`（`missing_meta` 仍对 filename/mtime 为 `true`）。

**验收标准**：
- 各模式解析正确（含时区换算、无秒、非法日期返回 None）。
- 无 EXIF 但有日期文件名的图片 → `time_source == "filename"` 且时间来自文件名。
- EXIF 存在时文件名不覆盖 EXIF（metadata 优先）。
- 真实数据 dry-run 的分片标题反映文件名时间而非 mtime。

---

## 附：YouTube 上传凭据准备与真实上传测试

### 一次性 OAuth 准备（Google Cloud Console）

> 上传（`videos.insert`）必须 OAuth 2.0，而 OAuth 客户端只能来自 Cloud Console。
> API Key 只读、Service Account 传不到个人频道，均不可替代。

1. **建项目**：console.cloud.google.com → 新建项目（如 `tubetape`）。
2. **开 API**：项目内搜索 "YouTube Data API v3" → 启用（免费，仅配额）。
3. **建 OAuth 客户端**：APIs & Services → 凭据 → 创建凭据 → **OAuth 客户端 ID → 桌面应用** → 下载 `client_secret.json`（本项目根目录已放置）。
4. **同意屏幕**：OAuth 同意屏幕 → External → 添加自己的邮箱为测试用户 → 加 scope：`youtube.force-ssl`（覆盖上传/删除重建/播放列表/读取；原 prompt.md 写的 `youtube.upload`+`youtube.readonly` 无法删片和写播放列表，已修正）。

**两个档位**：
- **Testing（免审核，最简单）**：保持 Testing 不发布 → 立即可用；refresh token 约 7 天过期，每周重跑登录命令。
- **In production（一劳永逸）**：点 "Publish app" → token 长期有效；自己（owner）使用无需 Google 审核（审核仅针对陌生用户）。

### 获取 token（无浏览器服务端也适用）

```bash
python scripts/oauth_login.py --client-secret client_secret.json --output token.json
```

脚本**打印一个授权 URL** → 你在任意浏览器打开并授权 → 把浏览器跳转到的 URL（含 `?code=...`）**粘贴回脚本** → 脚本兑换并写入 0600 权限的 `token.json`（含 refresh token）。全程无需本地服务器监听，服务端/本机都能用。

> 注意：Google 对桌面应用强制 PKCE，授权 URL 和兑换必须用同一个 flow 对象（脚本已保证），不能把 URL 交给另一个进程单独兑换。

### 真实上传测试（三层）

- **Mock**：`pytest tests/test_uploader.py`（零成本，无网络）。
- **dry-run**：`python -m tubetape.cli --dry-run ...`（不碰网络）。
- **单视频冒烟**：`TUBETAPE_TOKEN="$(cat token.json)" python scripts/upload_one.py test.mp4 "标题" --privacy private`，验证标题/章节/配额，然后手动删除。
- **完整流程**：`token.json` 放 db 同目录，或设 `TUBETAPE_TOKEN` 环境变量，去掉 `--dry-run` 真跑。

### 安全

- `client_secret.json` 与 `token.json` 都含机密，**不要提交到 git/公开仓库**（项目已配 `.gitignore`）。
- token 权限保持 0600（`oauth_login.py` 自动设置，`upload_one.py` 会检查告警）。

---

## 里程碑推进流程

1. 每轮执行：`/review-loop M<n>`（模板会读取本文件对应里程碑）。
2. 每轮审查对象：该里程碑交付文件 + `cli.py` + `test_cli.py`（以及新增的测试文件）。
3. 验收标准全部满足、`pytest` 全绿、reviewer 无 P0/P1 问题 → 进入下一里程碑。
4. 若一轮 `/review-loop` 后仍有未解决问题，再次运行 `/review-loop M<n>` 继续迭代（chain 是固定三步，不是真循环）。
