# TubeTape

[![Docker](https://img.shields.io/badge/docker-chet2026%2Ftubetape-blue)](https://hub.docker.com/r/chet2026/tubetape)
[![Python](https://img.shields.io/badge/python-3.10%2B-blue)](https://www.python.org/)
[![Platform](https://img.shields.io/badge/platform-linux%20%7C%20macos%20%7C%20windows-lightgrey)]()
[![License](https://img.shields.io/badge/license-see%20repo-green)](https://github.com/ouut/TubeTape)
[![YouTube API](https://img.shields.io/badge/YouTube%20API-Services-red)](https://www.youtube.com/t/terms)

**把家庭照片和视频自动整理成一段段视频，备份到你的 YouTube 私人频道。**
**Organize home photos & videos into segmented videos, backed up to your private YouTube channel.**

[🏠 中文](#中文) · [🌐 English](#english) · [隐私权政策 Privacy](Privacy_Policy.md) · [服务条款 Terms](Terms_of_Service.md) · [GitHub](https://github.com/ouut/TubeTape)

---

## 中文

### 这是什么

TubeTape 是一个**在你自己的电脑上运行的本地工具**。它扫描你指定的照片/视频目录，按**拍摄时间**把它们紧凑拼接、转码成一段段视频，再通过 **YouTube Data API** 上传到**你本人的** YouTube 频道（默认「私有」），并加入同一个按时间排序的播放列表，用于**备份和观看**。

它支持**持续运行**：往目录里扔新照片/视频，它会自动检测、自动归入对应分片、自动（必要时重建并重传）上传。

### 特点

- **归档优先**：每个文件恰好属于一个分片，分片严格按拍摄时间连续、内容完整。
- **本地运行**：扫描、解析、转码都在你本机完成，数据不上传到开发者服务器。
- **只操作你的频道**：使用你自己的 Google 授权，上传/管理你自己的 YouTube 频道。
- **保真优先**：画布取分片内最大尺寸（不降采样）、高画质编码、可选 4K/8K。
- **扫描缓存**：文件未变时跳过重读，重复扫描极快；扫描中断也不丢进度。
- **配额感知**：以 YouTube API 返回为准，配额用尽自动退避重试，无需人工干预。
- **持续监控**：`--watch` 挂着即可，新文件自动处理；退出时自动封片。

### 快速开始

```bash
# 1) 预览（只读，不转码不上传）
python3 -m tubetape --dry-run --no-watch -v \
  --input /path/to/photos --db /path/to/photos/tubetape.json --timezone Asia/Shanghai

# 2) 真实运行一次（处理完退出）
python3 -m tubetape --no-watch -v \
  --input /path/to/photos --db /path/to/photos/tubetape.json --timezone Asia/Shanghai

# 3) 确认无误后挂机
python3 -m tubetape --watch -v \
  --input /path/to/photos --db /path/to/photos/tubetape.json --timezone Asia/Shanghai
```

> Docker Compose 用户见仓库中的 `docker-compose.yml` 与 `.env.example`。

### 文档

- [隐私权政策](Privacy_Policy.md) / [Privacy Policy](Privacy_Policy.md)
- [服务条款](Terms_of_Service.md) / [Terms of Service](Terms_of_Service.md)

---

## English

### What it is

TubeTape is a **local tool that runs on your own computer**. It scans a directory of photos/videos you specify, organizes them by **capture time** into compact, transcoded segmented videos, and uploads them through the **YouTube Data API** to **your own** YouTube channel (default: private), adding them to a single capture-time-ordered playlist for **backup and viewing**.

It supports **continuous running**: drop new photos/videos into the directory and it detects, groups, and (when needed, by rebuilding and re-uploading) uploads them automatically.

### Features

- **Archive-first**: every file belongs to exactly one segment; segments stay time-ordered and complete.
- **Local-first**: scanning, parsing and transcoding all happen on your machine; no data is sent to the developer.
- **Operates only your channel**: uses your own Google authorization to upload to and manage your own YouTube channel.
- **Fidelity-first**: canvas takes the largest size in a segment (no downscaling), high-quality encoding, optional 4K/8K.
- **Scan cache**: unchanged files are not re-read, so repeat scans are very fast; interruptions don't lose progress.
- **Quota-aware**: driven by the YouTube API response — on quota exhaustion it backs off and retries automatically.
- **Continuous watch**: just leave `--watch` running; new files are processed automatically, with a flush on exit.

### Quick start

```bash
# 1) Preview (read-only; no transcode, no upload)
python3 -m tubetape --dry-run --no-watch -v \
  --input /path/to/photos --db /path/to/photos/tubetape.json --timezone Asia/Shanghai

# 2) One real run (exits when done)
python3 -m tubetape --no-watch -v \
  --input /path/to/photos --db /path/to/photos/tubetape.json --timezone Asia/Shanghai

# 3) Once confirmed, run continuously
python3 -m tubetape --watch -v \
  --input /path/to/photos --db /path/to/photos/tubetape.json --timezone Asia/Shanghai
```

> Docker Compose users: see `docker-compose.yml` and `.env.example` in the repository.

### Documents

- [Privacy Policy](Privacy_Policy.md)
- [Terms of Service](Terms_of_Service.md)

---

## 常见问题 / FAQ

**Q：`client_secret.json` 是什么？每次运行都要吗？ / What is `client_secret.json`? Is it needed every run?**

它是你**应用的身份凭据**，**只在登录时**用来向 Google 证明“是哪个应用在请求授权”。登录之后，`token.json` 已包含 `client_id`/`client_secret`/`refresh_token`，**平时运行只读 `token.json`，不需要 `client_secret.json`**。

It is your **app's OAuth client credential**, used **only when logging in**. After that, `token.json` already contains the client id/secret and refresh token, so **normal runs only need `token.json`.**

**Q：没有 token，Docker 里怎么登录？ / How do I log in inside Docker without a token?**

```bash
docker compose run --rm tubetape \
  --db /db/tubetape.json --client-secret /db/client_secret.json --login
```

打印授权 URL → 浏览器同意 → 把跳转回 `http://localhost:8080/?code=...` 的整条 URL 粘回终端 → 生成 `/db/token.json`。必须**交互式**运行（不能用 `up -d`）。

**Q：会操作别人的 YouTube 频道吗？ / Does it operate on someone else's channel?**

不会。上传、删除、加播放列表操作的都是 **token 所属的那个频道**；每个人用**自己的** token，操作**自己的**频道。你的 `token.json` 不会被分发给别人。

No. It operates **the channel the token belongs to**; everyone uses **their own** token, so **their own** channel. Your `token.json` is never distributed.

---

## 联系方式 / Contact

- **开发者 / Developer:** sxxwff 团队 / sxxwff team
- **邮箱 / Email:** sxxwff@gmail.com
- **源码 / Source:** <https://github.com/ouut/TubeTape>
