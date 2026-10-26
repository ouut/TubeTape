FROM python:3.11-slim

# ffmpeg: 转码 + 视频元数据探测；tzdata: 时区；ca-certificates: HTTPS(YouTube API)
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg tzdata ca-certificates && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 只拷贝运行所需文件，pip install 会自动装 Pillow/rich/google-api-client 等运行时依赖
COPY pyproject.toml ./
COPY tubetape/ tubetape/
RUN pip install --no-cache-dir . && \
    mkdir -p /data && \
    printf '#!/bin/sh\nset -e\nhas_input=0\nhas_db=0\nfor arg in "$@"; do\n  case "$arg" in\n    --input|--input=*) has_input=1 ;;\n    --db|--db=*) has_db=1 ;;\n  esac\ndone\nif [ "$has_db" -eq 0 ] && [ -d /db ]; then\n  set -- --db /db/tubetape.json "$@"\nfi\nif [ "$has_input" -eq 0 ] && [ -d /data ]; then\n  set -- --input /data "$@"\nfi\nexec tubetape "$@"\n' > /usr/local/bin/docker-entrypoint.sh && \
    chmod +x /usr/local/bin/docker-entrypoint.sh

WORKDIR /data

# 入口：默认以 /data 为媒体目录；若挂载了 /db 则自动将数据库指向 /db/tubetape.json，否则落在 /data/tubetape.json
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
