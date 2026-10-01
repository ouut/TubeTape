FROM python:3.11-slim

# ffmpeg: 转码 + 视频元数据探测；tzdata: 时区；ca-certificates: HTTPS(YouTube API)
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg tzdata ca-certificates && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 只拷贝运行所需文件，pip install 会自动装 Pillow/rich/google-api-client 等运行时依赖
COPY pyproject.toml ./
COPY tubetape/ tubetape/
RUN pip install --no-cache-dir .

# 入口：tubetape 命令，所有 CLI 参数直接透传
ENTRYPOINT ["tubetape"]
