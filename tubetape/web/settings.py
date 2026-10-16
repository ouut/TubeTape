"""Settings management page for TubeTape Web UI."""

from __future__ import annotations

from .common import COMMON_CSS, render_navbar

_SETTINGS_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>系统配置 · TubeTape</title>
  <style>
    __COMMON_CSS__

    .settings-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      margin-bottom: 24px;
      flex-wrap: wrap;
      gap: 14px;
    }

    .config-section {
      background: var(--bg-card);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 24px;
      margin-bottom: 24px;
    }
    .section-title {
      font-size: 1.1rem;
      font-weight: 700;
      color: #fff;
      display: flex;
      align-items: center;
      gap: 10px;
      margin-bottom: 4px;
    }
    .section-desc {
      font-size: 0.8rem;
      color: var(--text-muted);
      margin-bottom: 20px;
    }

    .form-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
      gap: 20px;
    }

    .form-group {
      display: flex;
      flex-direction: column;
      gap: 6px;
    }
    .form-label {
      font-size: 0.85rem;
      font-weight: 600;
      color: #e5e7eb;
      display: flex;
      align-items: center;
      justify-content: space-between;
    }
    .form-help {
      font-size: 0.75rem;
      color: var(--text-muted);
      line-height: 1.4;
    }

    .form-control {
      width: 100%;
      padding: 8px 12px;
      background: rgba(0, 0, 0, 0.35);
      border: 1px solid var(--border);
      border-radius: 6px;
      color: #fff;
      font-size: 0.88rem;
      transition: border-color 0.15s ease;
    }
    .form-control:focus {
      outline: none;
      border-color: var(--primary);
      background: rgba(0, 0, 0, 0.5);
    }

    /* Toggle switch row */
    .toggle-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 10px 14px;
      background: rgba(255, 255, 255, 0.02);
      border-radius: 8px;
      border: 1px solid var(--border);
    }

    /* Bottom Sticky Action Bar */
    .sticky-actions {
      position: sticky;
      bottom: 0;
      background: rgba(15, 23, 42, 0.95);
      backdrop-filter: blur(12px);
      border-top: 1px solid var(--border);
      padding: 16px 24px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      z-index: 500;
      margin: 0 -24px -24px -24px;
      border-bottom-left-radius: 0;
      border-bottom-right-radius: 0;
    }
  </style>
</head>
<body>
  __NAVBAR__

  <div class="container" style="padding-bottom: 90px;">
    <div class="settings-header">
      <div>
        <h2 style="font-size: 1.35rem; font-weight: 700;">⚙️ 系统参数全量配置</h2>
        <div style="font-size: 0.82rem; color: var(--text-muted); margin-top: 4px;">
          所有参数均支持在线调整并持久化保存至 <code>tubetape.json</code>。点击“应用配置”将中断并以新参数平滑重启管道。
        </div>
      </div>
      <div>
        <button class="btn btn-emerald" onclick="saveAndApplyConfig()">💾 应用配置并重启调度器</button>
      </div>
    </div>

    <!-- Section 1: 路径与持久化 -->
    <div class="config-section">
      <div class="section-title">📁 路径与构建存储配置</div>
      <div class="section-desc">指定媒体源目录、Build 转码缓存目录及元数据存储路径。</div>
      <div class="form-grid">
        <div class="form-group">
          <label class="form-label">媒体源根目录 (input)</label>
          <input type="text" id="cfg-input" class="form-control" placeholder="如: /data/photos 或 F:\\photos">
          <span class="form-help">存放所有照片与视频的根目录，程序将从此目录递归发现媒体文件。</span>
        </div>
        <div class="form-group">
          <label class="form-label">分段构建输出目录 (segments_dir)</label>
          <input type="text" id="cfg-segments_dir" class="form-control" placeholder="如: /data/uploaded_segments">
          <span class="form-help">转码生成的分片 MP4 临时文件存放目录（默认位于数据库同级目录下）。</span>
        </div>
        <div class="form-group">
          <label class="form-label">数据库文件路径 (db)</label>
          <input type="text" id="cfg-db" class="form-control" placeholder="tubetape.json">
          <span class="form-help">持久化媒体索引、分片记录与配置信息的 JSON 数据库文件路径。</span>
        </div>
        <div class="form-group">
          <label class="form-label">日志文件路径 (log_file)</label>
          <input type="text" id="cfg-log_file" class="form-control" placeholder="留空使用默认: <db>.log">
          <span class="form-help">记录全部详细转码与上传流程的日志路径。</span>
        </div>
        <div class="form-group">
          <label class="form-label">本地分片保留数 (keep_segments)</label>
          <input type="number" id="cfg-keep_segments" class="form-control" min="0" step="1">
          <span class="form-help">上传成功后本地保留最新分段视频文件的数量；设为 0 表示上传后立即删除以节约磁盘。</span>
        </div>
        <div class="form-group">
          <label class="form-label">Web 控制台端口 (web_port)</label>
          <input type="number" id="cfg-web_port" class="form-control" min="1024" max="65535">
          <span class="form-help">当前管理控制台与画廊服务的 HTTP 监听端口（默认 8080）。</span>
        </div>
      </div>
    </div>

    <!-- Section 2: 视频转码与画质 -->
    <div class="config-section">
      <div class="section-title">⚡ 视频画质与转码引擎</div>
      <div class="section-desc">调节 FFmpeg 编码质量、分辨率限制、硬件加速以及图片运镜参数。</div>
      <div class="form-grid">
        <div class="form-group">
          <label class="form-label">单分段最大时长 (segment_duration)</label>
          <input type="text" id="cfg-segment_duration" class="form-control" placeholder="如: 20m, 1h, 1200s">
          <span class="form-help">单个聚合视频的上限播放时长，支持友好单位（如 20m, 1h）。</span>
        </div>
        <div class="form-group">
          <label class="form-label">视频编码器 (encoder)</label>
          <select id="cfg-encoder" class="form-control">
            <option value="auto">auto (自动检测最佳硬件加速)</option>
            <option value="h264_nvenc">h264_nvenc (NVIDIA GPU 硬件加速)</option>
            <option value="h264_videotoolbox">h264_videotoolbox (Apple Silicon / Mac GPU)</option>
            <option value="h264_qsv">h264_qsv (Intel QuickSync 硬件加速)</option>
            <option value="libx264">libx264 (纯 CPU 软件编码 - 兼容性最高)</option>
          </select>
          <span class="form-help">优先建议使用 auto，会自动按 NVIDIA -> Apple -> Intel -> CPU 顺位自适应。</span>
        </div>
        <div class="form-group">
          <label class="form-label">画质等级 CRF (crf)</label>
          <input type="number" id="cfg-crf" class="form-control" min="0" max="51" step="1">
          <span class="form-help">H.264 恒定画质参数（0-51，数值越小画质越高，推荐 18 原画级别）。</span>
        </div>
        <div class="form-group">
          <label class="form-label">编码速度预设 (x264_preset)</label>
          <select id="cfg-x264_preset" class="form-control">
            <option value="ultrafast">ultrafast (最快)</option>
            <option value="superfast">superfast</option>
            <option value="veryfast">veryfast</option>
            <option value="faster">faster</option>
            <option value="fast">fast (默认推荐)</option>
            <option value="medium">medium</option>
            <option value="slow">slow</option>
            <option value="slower">slower</option>
            <option value="veryslow">veryslow (压缩率最高)</option>
          </select>
          <span class="form-help">平衡编码耗时与文件体积，推荐 fast。</span>
        </div>
        <div class="form-group">
          <label class="form-label">最高分辨率上限 (max_resolution)</label>
          <input type="text" id="cfg-max_resolution" class="form-control" placeholder="3840x2160">
          <span class="form-help">视频画布宽高上限（从不放大超分，原始小于该分辨率的保持原始包围盒）。</span>
        </div>
        <div class="form-group">
          <label class="form-label">画布适配模式 (canvas_mode)</label>
          <select id="cfg-canvas_mode" class="form-control">
            <option value="max">max (自适应最大包围盒 - 画面不裁切无缩小)</option>
            <option value="first">first (沿用分段内第一张文件的分辨率)</option>
          </select>
          <span class="form-help">推荐 max 包围盒模式，确保纵向、横向照片与视频都完整呈现。</span>
        </div>
        <div class="form-group">
          <label class="form-label">输出帧率 (fps)</label>
          <input type="number" id="cfg-fps" class="form-control" min="1" max="120" step="1">
          <span class="form-help">视频帧率，静态相册默认推荐 30fps。</span>
        </div>
        <div class="form-group">
          <label class="form-label">单张照片展示时长 (image_duration)</label>
          <input type="number" id="cfg-image_duration" class="form-control" min="0.5" max="60" step="0.5">
          <span class="form-help">每张照片在生成视频中停留的秒数（默认 3 秒）。</span>
        </div>
        <div class="form-group">
          <div class="toggle-row" style="margin-top: 18px;">
            <div>
              <div style="font-weight: 600; font-size: 0.85rem; color: #fff;">Ken Burns 照片运镜特效</div>
              <div style="font-size: 0.75rem; color: var(--text-muted);">为静态照片启用缓慢平移与微放大动画效果</div>
            </div>
            <label class="toggle-switch">
              <input type="checkbox" id="cfg-ken_burns">
              <span class="toggle-slider"></span>
            </label>
          </div>
        </div>
      </div>
    </div>

    <!-- Section 3: 扫描与过滤规则 -->
    <div class="config-section">
      <div class="section-title">🔍 媒体扫描与过滤规则</div>
      <div class="section-desc">设定对短视频聚合、实况照片伴随视频以及特定设备的过滤条件。</div>
      <div class="form-grid">
        <div class="form-group">
          <label class="form-label">短视频聚合阈值 (short_video_threshold)</label>
          <input type="text" id="cfg-short_video_threshold" class="form-control" placeholder="如: 300s, 5m">
          <span class="form-help">时长低于或等于此阈值的视频与照片混排；长于此时长的视频将作为独立单视频上传。</span>
        </div>
        <div class="form-group">
          <label class="form-label">过滤废片最短时长 (min_video_duration)</label>
          <input type="text" id="cfg-min_video_duration" class="form-control" placeholder="如: 2s">
          <span class="form-help">短于此秒数的微短视频将被直接跳过（避免误触录制的废片占用分片）。</span>
        </div>
        <div class="form-group">
          <label class="form-label">EXIF 时区基准 (timezone)</label>
          <input type="text" id="cfg-timezone" class="form-control" placeholder="Asia/Shanghai">
          <span class="form-help">用于解释照片 EXIF 中未标注时区的时间戳（默认 Asia/Shanghai）。</span>
        </div>
      </div>

      <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 14px; margin-top: 16px;">
        <div class="toggle-row">
          <div>
            <div style="font-weight: 600; font-size: 0.85rem; color: #fff;">忽略 Apple 实况照片视频</div>
            <div style="font-size: 0.75rem; color: var(--text-muted);">自动跳过与照片同名且&lt;=3.5s的伴生视频</div>
          </div>
          <label class="toggle-switch">
            <input type="checkbox" id="cfg-ignore_live_photos">
            <span class="toggle-slider"></span>
          </label>
        </div>
        <div class="toggle-row">
          <div>
            <div style="font-weight: 600; font-size: 0.85rem; color: #fff;">仅包含相机照片</div>
            <div style="font-size: 0.75rem; color: var(--text-muted);">仅保留含 Make/Model 的相机拍摄照，忽略截图与网络图</div>
          </div>
          <label class="toggle-switch">
            <input type="checkbox" id="cfg-only_camera_photos">
            <span class="toggle-slider"></span>
          </label>
        </div>
        <div class="toggle-row">
          <div>
            <div style="font-weight: 600; font-size: 0.85rem; color: #fff;">仅包含手机录像</div>
            <div style="font-size: 0.75rem; color: var(--text-muted);">仅保留含相机设备元数据的手机拍摄录像</div>
          </div>
          <label class="toggle-switch">
            <input type="checkbox" id="cfg-only_phone_videos">
            <span class="toggle-slider"></span>
          </label>
        </div>
      </div>
    </div>

    <!-- Section 4: 上传与监控服务 -->
    <div class="config-section">
      <div class="section-title">☁️ YouTube 上传与后台监控</div>
      <div class="section-desc">配置是否上传到 YouTube 频道、隐私状态及本地文件变动监控行为。</div>
      <div class="form-grid">
        <div class="form-group">
          <label class="form-label">YouTube 视频隐私状态 (privacy)</label>
          <select id="cfg-privacy" class="form-control">
            <option value="private">private (私享 - 仅自己可见)</option>
            <option value="unlisted">unlisted (不公开列出 - 拥有链接者可见)</option>
          </select>
          <span class="form-help">上传至 YouTube 后的初始隐私级别。</span>
        </div>
        <div class="form-group">
          <label class="form-label">指定播放列表 ID (playlist)</label>
          <input type="text" id="cfg-playlist" class="form-control" placeholder="可选: 留空不自动加入播放列表">
          <span class="form-help">上传成功后自动按拍摄时间正序加入的 YouTube 播放列表 ID。</span>
        </div>
        <div class="form-group">
          <label class="form-label">文件变动静默期 (quiet_period)</label>
          <input type="text" id="cfg-quiet_period" class="form-control" placeholder="如: 10m">
          <span class="form-help">监听模式下，最后一次文件变动后等待此时长确保写入稳定再开始处理。</span>
        </div>
        <div class="form-group">
          <label class="form-label">轮询检测周期 (poll_interval)</label>
          <input type="text" id="cfg-poll_interval" class="form-control" placeholder="如: 30s">
          <span class="form-help">后台调度器检查时间状态的心跳周期。</span>
        </div>
        <div class="form-group">
          <label class="form-label">目录安全扫描周期 (mtime_interval)</label>
          <input type="text" id="cfg-mtime_interval" class="form-control" placeholder="如: 10m">
          <span class="form-help">针对不支持 inotify 的网络共享盘的兜底轮询间隔。</span>
        </div>
        <div class="form-group">
          <label class="form-label">配额耗尽退避时长 (quota_backoff)</label>
          <input type="text" id="cfg-quota_backoff" class="form-control" placeholder="如: 1h">
          <span class="form-help">当 YouTube API 每日配额耗尽时，暂停并等待重新尝试的时长。</span>
        </div>
      </div>

      <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 14px; margin-top: 16px;">
        <div class="toggle-row">
          <div>
            <div style="font-weight: 600; font-size: 0.85rem; color: #fff;">仅本地转码 (no_upload)</div>
            <div style="font-size: 0.75rem; color: var(--text-muted);">开启后仅生成本地 MP4 视频，不向 YouTube 上传</div>
          </div>
          <label class="toggle-switch">
            <input type="checkbox" id="cfg-no_upload">
            <span class="toggle-slider"></span>
          </label>
        </div>
        <div class="toggle-row">
          <div>
            <div style="font-weight: 600; font-size: 0.85rem; color: #fff;">启用文件变动实时监控 (watch)</div>
            <div style="font-size: 0.75rem; color: var(--text-muted);">开启后持续监控相册目录，发现新照片自动规划构建</div>
          </div>
          <label class="toggle-switch">
            <input type="checkbox" id="cfg-watch">
            <span class="toggle-slider"></span>
          </label>
        </div>
      </div>
    </div>

    <!-- Sticky Bottom Bar -->
    <div class="sticky-actions">
      <div style="font-size: 0.85rem; color: var(--text-muted);">
        修改配置后将立即中断当前所有后台操作，并按最新参数重新运行调度器。
      </div>
      <div style="display: flex; gap: 12px;">
        <button class="btn" onclick="loadConfig()">🔄 放弃更改并重置</button>
        <button class="btn btn-emerald" onclick="saveAndApplyConfig()">💾 应用配置并重启调度器</button>
      </div>
    </div>
  </div>

  <script>
    let currentConfig = {};

    async function loadConfig() {
      try {
        const res = await fetch('/api/config');
        if (!res.ok) throw new Error('拉取配置失败');
        const d = await res.json();
        currentConfig = d.config || {};

        // Populate Form
        setVal('cfg-input', currentConfig.input);
        setVal('cfg-segments_dir', currentConfig.segments_dir);
        setVal('cfg-db', currentConfig.db);
        setVal('cfg-log_file', currentConfig.log_file);
        setVal('cfg-keep_segments', currentConfig.keep_segments ?? 0);
        setVal('cfg-web_port', currentConfig.web_port ?? 8080);

        setVal('cfg-segment_duration', formatDuration(currentConfig.segment_duration));
        setVal('cfg-encoder', currentConfig.encoder || 'auto');
        setVal('cfg-crf', currentConfig.crf ?? 18);
        setVal('cfg-x264_preset', currentConfig.x264_preset || 'fast');
        setVal('cfg-max_resolution', currentConfig.max_resolution || '3840x2160');
        setVal('cfg-canvas_mode', currentConfig.canvas_mode || 'max');
        setVal('cfg-fps', currentConfig.fps ?? 30);
        setVal('cfg-image_duration', currentConfig.image_duration ?? 3);
        setBool('cfg-ken_burns', currentConfig.ken_burns);

        setVal('cfg-short_video_threshold', formatDuration(currentConfig.short_video_threshold));
        setVal('cfg-min_video_duration', formatDuration(currentConfig.min_video_duration));
        setVal('cfg-timezone', currentConfig.timezone || 'Asia/Shanghai');
        setBool('cfg-ignore_live_photos', currentConfig.ignore_live_photos ?? true);
        setBool('cfg-only_camera_photos', currentConfig.only_camera_photos ?? false);
        setBool('cfg-only_phone_videos', currentConfig.only_phone_videos ?? false);

        setVal('cfg-privacy', currentConfig.privacy || 'private');
        setVal('cfg-playlist', currentConfig.playlist || '');
        setVal('cfg-quiet_period', formatDuration(currentConfig.quiet_period));
        setVal('cfg-poll_interval', formatDuration(currentConfig.poll_interval));
        setVal('cfg-mtime_interval', formatDuration(currentConfig.mtime_interval));
        setVal('cfg-quota_backoff', formatDuration(currentConfig.quota_backoff));
        setBool('cfg-no_upload', currentConfig.no_upload ?? false);
        setBool('cfg-watch', currentConfig.watch ?? true);

        showToast('已加载最新配置');
      } catch (err) {
        showToast('❌ 加载失败: ' + err.message, true);
      }
    }

    function setVal(id, val) {
      const el = document.getElementById(id);
      if (el) el.value = val !== undefined && val !== null ? val : '';
    }
    function setBool(id, val) {
      const el = document.getElementById(id);
      if (el) el.checked = Boolean(val);
    }

    function formatDuration(sec) {
      if (!sec) return '';
      if (sec % 3600 === 0) return (sec / 3600) + 'h';
      if (sec % 60 === 0) return (sec / 60) + 'm';
      return sec + 's';
    }

    async function saveAndApplyConfig() {
      if (!confirm('确定要应用新配置吗？系统将中断当前正在执行的任务，并按新配置重新启动流水线。')) return;

      const payload = {
        input: document.getElementById('cfg-input').value.trim(),
        segments_dir: document.getElementById('cfg-segments_dir').value.trim(),
        db: document.getElementById('cfg-db').value.trim(),
        log_file: document.getElementById('cfg-log_file').value.trim(),
        keep_segments: parseInt(document.getElementById('cfg-keep_segments').value, 10) || 0,
        web_port: parseInt(document.getElementById('cfg-web_port').value, 10) || 8080,

        segment_duration: document.getElementById('cfg-segment_duration').value.trim(),
        encoder: document.getElementById('cfg-encoder').value,
        crf: parseInt(document.getElementById('cfg-crf').value, 10) || 18,
        x264_preset: document.getElementById('cfg-x264_preset').value,
        max_resolution: document.getElementById('cfg-max_resolution').value.trim(),
        canvas_mode: document.getElementById('cfg-canvas_mode').value,
        fps: parseInt(document.getElementById('cfg-fps').value, 10) || 30,
        image_duration: parseFloat(document.getElementById('cfg-image_duration').value) || 3.0,
        ken_burns: document.getElementById('cfg-ken_burns').checked,

        short_video_threshold: document.getElementById('cfg-short_video_threshold').value.trim(),
        min_video_duration: document.getElementById('cfg-min_video_duration').value.trim(),
        timezone: document.getElementById('cfg-timezone').value.trim(),
        ignore_live_photos: document.getElementById('cfg-ignore_live_photos').checked,
        only_camera_photos: document.getElementById('cfg-only_camera_photos').checked,
        only_phone_videos: document.getElementById('cfg-only_phone_videos').checked,

        privacy: document.getElementById('cfg-privacy').value,
        playlist: document.getElementById('cfg-playlist').value.trim(),
        quiet_period: document.getElementById('cfg-quiet_period').value.trim(),
        poll_interval: document.getElementById('cfg-poll_interval').value.trim(),
        mtime_interval: document.getElementById('cfg-mtime_interval').value.trim(),
        quota_backoff: document.getElementById('cfg-quota_backoff').value.trim(),
        no_upload: document.getElementById('cfg-no_upload').checked,
        watch: document.getElementById('cfg-watch').checked
      };

      try {
        const res = await fetch('/api/config/apply', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(payload)
        });
        const d = await res.json();
        if (res.ok && d.ok) {
          alert('✅ ' + (d.message || '配置已应用，后台处理流水线已平滑重新启动！'));
          loadConfig();
        } else {
          alert('❌ 保存配置失败: ' + (d.error || '未知错误'));
        }
      } catch (err) {
        alert('❌ 请求异常: ' + err.message);
      }
    }

    loadConfig();
  </script>
</body>
</html>"""

def render_settings_html() -> str:
    """Render full HTML for the Settings management page."""
    navbar = render_navbar(active_page="settings")
    return _SETTINGS_HTML_TEMPLATE.replace("__COMMON_CSS__", COMMON_CSS).replace("__NAVBAR__", navbar)
