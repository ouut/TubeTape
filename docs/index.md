# TubeTape

把家庭照片和视频自动整理成一段段视频，上传到你的 **YouTube 私人频道**，用于备份和观看。

TubeTape 是一个**在你自己电脑上运行的本地工具**：它扫描你指定的照片/视频目录，按拍摄时间把它们整理、拼接并转码成一段段视频，再通过 YouTube Data API 上传到**你本人的** YouTube 频道（默认私有），并加入同一个按时间排序的播放列表。它支持持续运行，新照片/视频会自动检测、自动归入分片、自动上传。

---

## 文档

- [隐私权政策 / Privacy Policy](Privacy_Policy.md)
- [服务条款 / Terms of Service](Terms_of_Service.md)

---

## 快速了解

- **本地运行**：不上传你的数据到开发者服务器；扫描、解析、转码都在你本机完成。
- **只操作你自己的频道**：通过你自己的 Google 授权（`youtube.force-ssl`），上传到你自己的 YouTube 频道。
- **归档优先**：每个文件恰好属于一个分片，分片按拍摄时间连续、内容完整。
- **持续监控**：`--watch` 挂着即可，新文件自动处理。

> 📦 源码与使用说明：<https://github.com/ouut/TubeTape>

---

## 联系方式

- **开发者 / 团队：** sxxwff 团队
- **联系邮箱：** sxxwff@gmail.com
