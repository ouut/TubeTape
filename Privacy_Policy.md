# 隐私权政策 (Privacy Policy)

**生效日期：** 2026 年 10 月 1 日  
**最近更新日期：** 2026 年 10 月 3 日  

本隐私权政策适用于由 **sxxwff 团队**（以下简称“我们”）开发的 **TubeTape**（以下简称“本应用”）。

**本应用是一个在您自己的电脑上运行的本地工具**：它把您本地的照片和视频整理、拼接并转码为一段段视频，并通过 Google / YouTube API 上传到**您本人的 YouTube 频道**（默认隐私状态为“私有”），用于个人备份与观看。

- **官方网站：** <https://ouut.github.io/TubeTape/>
- **联系邮箱：** sxxwff@gmail.com

---

## 1. 我们访问与处理的信息

本应用**不运营任何服务器**，也不会把您的数据发送给我们（开发者）。所有扫描、元数据解析和视频转码都在您本地的机器上完成。

### 1.1 本地文件（仅在您的设备上处理）

- 您指定目录下的照片/视频：读取其内容哈希、拍摄时间、分辨率、时长等元数据，用于整理与分片；
- 处理进度与索引保存在**您本地的** `tubetape.json` 数据库，运行日志保存在**您本地的** `tubetape.json.log`；
- 转码出的分片视频只是临时文件，上传成功后即从本地删除。

这些数据**始终留在您的设备上**，我们无法访问。

### 1.2 通过 Google / YouTube 授权的数据

本应用请求以下 Google OAuth 权限范围：

| 权限范围 | 用途 |
|---|---|
| `https://www.googleapis.com/auth/youtube.force-ssl` | 向**您本人**的 YouTube 频道上传视频、将视频加入播放列表，以及在重建分片时**删除被替换的旧视频** |

通过该权限，本应用会访问：

- 您频道的**上传列表**（用于“上传对账”，避免重复上传）；
- 由本应用上传视频的**标题、简介（含内部标记）与视频 ID**。

本应用**不会**读取您的 Google 姓名、电子邮箱、头像等个人资料，也**不会**访问除您本人 YouTube 频道内容之外的任何 Google 数据。

### 1.3 凭据的存储

OAuth 令牌（含 refresh token）保存在**您本地的** `token.json` 文件中，文件权限被设为**仅所有者可读（0600）**。该文件为**明文**存储，请您妥善保管、不要分享或提交到代码仓库。我们（开发者）**无法访问**它。

---

## 2. 信息的使用目的

- 整理并生成分片视频，上传到您自己的 YouTube 频道；
- 记录处理进度，以支持断点续传与增量处理；
- 运行日志，用于排错。

**特别声明：** 我们不会将通过 Google API 获取的数据用于投放广告、构建用户画像，或用于本应用核心功能之外的任何目的，也不会出售或转让给第三方。

---

## 3. Google API 用户数据有限使用声明 (Google API Limited Use Disclosure)

本应用对从 Google API 接收到的信息的使用与传输，严格遵守
[Google API 服务用户数据政策 (Google API Services User Data Policy)](https://developers.google.com/terms/api-services-user-data-policy)，包括其中的**有限使用 (Limited Use)** 规定：

- **不转让给第三方：** 不会出售、出租或转让您的 Google 数据（法律强制要求或经您明确同意除外）；
- **不用于广告：** 不用于投放个性化广告；
- **不用于模型训练：** 不使用通过 Google API 获取的数据来训练通用机器学习或人工智能模型。

---

## 4. 数据的共享与披露

除以下情形外，我们不会向任何第三方共享您的信息：

1. **依照法律要求**（传票、法院命令或适用法律）；
2. **经您明确授权**。

您上传的视频存储于 YouTube，适用 Google / YouTube 的相关政策。

---

## 5. 数据存储与安全

- **以本地为主：** 除上传到您自己 YouTube 频道的视频外，所有数据（数据库、日志、令牌、临时转码文件）都只存在于您的设备上；
- **传输安全：** 与 Google API 的通信使用 HTTPS/TLS；
- **令牌保护：** `token.json` 以仅所有者可读权限（0600）存储（明文，请注意保管）；
- **保留期限：** 本地数据完全由您掌控，您可随时删除 `tubetape.json`、`tubetape.json.log`、`token.json` 以及上传的视频。

---

## 6. 您的权利与撤销授权

- **撤销授权：** 前往 [Google 账户 - 第三方访问权限](https://myaccount.google.com/permissions) 撤销本应用的访问权限。撤销后，本应用将立即停止访问您的 Google 数据；
- **删除数据：** 删除本地的 `tubetape.json` / `token.json` / 日志即可移除本地数据；您上传的视频可在 YouTube Studio 中自行删除。

---

## 7. 本应用与 YouTube API 服务

本应用使用 **YouTube API 服务**。

- 使用本应用即表示您同意受 [YouTube 服务条款](https://www.youtube.com/t/terms) 约束；
- 关于 Google 如何收集和处理数据，请参见 [Google 隐私权政策](http://www.google.com/policies/privacy)。

---

## 8. 政策更新

我们可能不定期更新本隐私权政策。任何重大变更都会在本页面发布并更新“最近更新日期”。

---

## 9. 联系我们

- **开发者 / 团队：** sxxwff 团队
- **联系邮箱：** sxxwff@gmail.com
- **官方网站：** <https://ouut.github.io/TubeTape/>

---
---

# Privacy Policy (English)

**Effective date:** October 1, 2026  
**Last updated:** October 3, 2026  

This Privacy Policy applies to **TubeTape** (the “App”), developed by the **sxxwff team** (“we”, “us”).

**The App is a local tool that runs on your own computer.** It organizes your local photos and videos into segmented videos and uploads them, through the Google / YouTube API, to **your own YouTube channel** (default privacy status: private), for personal backup and viewing.

- **Website:** <https://ouut.github.io/TubeTape/>
- **Contact email:** sxxwff@gmail.com

---

## 1. Information We Access and Process

The App **does not operate any server** and does not send your data to us (the developer). All scanning, metadata parsing and video transcoding happen locally on your machine.

### 1.1 Local files (processed only on your device)

- Photos/videos in the directory you specify: we read their content hash, capture time, resolution and duration for organizing and segmenting;
- Processing progress and the index are stored in **your local** `tubetape.json` database, and run logs in **your local** `tubetape.json.log`;
- The transcoded segment videos are temporary and are deleted locally after a successful upload.

This data **always stays on your device**; we cannot access it.

### 1.2 Data obtained through Google / YouTube authorization

The App requests the following Google OAuth scope:

| Scope | Purpose |
|---|---|
| `https://www.googleapis.com/auth/youtube.force-ssl` | Upload videos to **your own** YouTube channel, add videos to a playlist, and delete the old video that is replaced when a segment is rebuilt |

Through this scope, the App accesses:

- your channel's **uploads list** (used for “reconciliation” to avoid duplicate uploads);
- the **titles, descriptions (including an internal marker) and video IDs** of videos uploaded by the App.

The App does **not** read your Google name, email address, profile photo or other profile data, and does **not** access any Google data beyond your own YouTube channel content.

### 1.3 Credential storage

The OAuth token (including the refresh token) is stored in **your local** `token.json` file, with permissions set to **owner-only readable (0600)**. The file is stored in **plain text**; please keep it safe and do not share it or commit it to a repository. We (the developer) **cannot access** it.

---

## 2. How We Use Information

- Organize and generate segmented videos and upload them to your own YouTube channel;
- Record processing progress to support resumable and incremental processing;
- Run logs for troubleshooting.

**Special notice:** We do not use data obtained through the Google API for advertising or user profiling, or for any purpose beyond the App's core functionality, and we do not sell or transfer it to third parties.

---

## 3. Google API Limited Use Disclosure

The App's use and transfer of information received from Google APIs adheres to the [Google API Services User Data Policy](https://developers.google.com/terms/api-services-user-data-policy), including the **Limited Use** requirements:

- **No transfer to third parties:** we do not sell, rent or transfer your Google data (except as required by law or with your explicit consent);
- **No advertising:** not used for personalized advertising;
- **No model training:** not used to train general machine learning or AI models.

---

## 4. Sharing and Disclosure

We do not share your information with any third party except:

1. **as required by law** (subpoena, court order or applicable law);
2. **with your explicit authorization**.

Videos you upload are stored on YouTube and are subject to Google / YouTube policies.

---

## 5. Data Storage and Security

- **Primarily local:** except for videos uploaded to your own YouTube channel, all data (database, logs, token, temporary transcode files) exists only on your device;
- **Transit security:** communication with Google APIs uses HTTPS/TLS;
- **Token protection:** `token.json` is stored with owner-only permissions (0600) in plain text (please keep it safe);
- **Retention:** local data is fully under your control; you may delete `tubetape.json`, `tubetape.json.log`, `token.json` and your uploaded videos at any time.

---

## 6. Your Rights and Revoking Access

- **Revoke access:** go to [Google Account – Third-party access](https://myaccount.google.com/permissions) to revoke the App. After revocation, the App will immediately stop accessing your Google data;
- **Delete data:** delete your local `tubetape.json` / `token.json` / logs to remove local data; uploaded videos can be deleted by you in YouTube Studio.

---

## 7. The App and YouTube API Services

The App uses **YouTube API Services**.

- By using the App, you agree to be bound by the [YouTube Terms of Service](https://www.youtube.com/t/terms);
- To learn how Google collects and processes data, see the [Google Privacy Policy](http://www.google.com/policies/privacy).

---

## 8. Changes to This Policy

We may update this Privacy Policy from time to time. Any material changes will be posted on this page and the “Last updated” date will be revised.

---

## 9. Contact Us

- **Developer / Team:** sxxwff team
- **Contact email:** sxxwff@gmail.com
- **Website:** <https://ouut.github.io/TubeTape/>
