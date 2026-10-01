#!/usr/bin/env bash
# 构建 TubeTape Docker 镜像，并在构建完成后直接推送到 Docker Hub。
#
# 默认镜像地址：chet2026/tubetape（可用环境变量 TUBETAPE_IMAGE 覆盖）。
# 默认行为：构建 + 推送（推送前请先 `docker login`）。
#
# 用法：
#   ./scripts/docker_build.sh                     # 构建并推送 chet2026/tubetape:latest（当前架构）
#   ./scripts/docker_build.sh v0.1.1              # 构建并推送指定 tag
#   ./scripts/docker_build.sh v0.1.1 --no-push    # 只构建不推送（本地测试）
#   ./scripts/docker_build.sh latest --multi      # 多架构 amd64+arm64 构建并推送（需 buildx）
#   TUBETAPE_IMAGE=myhub/tubetape ./scripts/docker_build.sh v0.1.1
set -euo pipefail

IMAGE="${TUBETAPE_IMAGE:-chet2026/tubetape}"
TAG="${1:-latest}"
PUSH="1"
MULTI=""

for arg in "${@:2}"; do
    case "$arg" in
        --push)    PUSH="1" ;;   # 兼容旧写法（默认即推送）
        --no-push) PUSH="" ;;
        --multi)   MULTI="1" ;;
        *) echo "未知参数: $arg" >&2; exit 2 ;;
    esac
done

cd "$(dirname "$0")/.."

if [ -n "$MULTI" ]; then
    args=(buildx build --platform linux/amd64,linux/arm64 -t "${IMAGE}:${TAG}")
    if [ -n "$PUSH" ]; then
        args+=(--push)
        echo "多架构构建并推送: linux/amd64, linux/arm64 -> ${IMAGE}:${TAG}"
    else
        echo "多架构构建(仅 buildx 缓存, 不推送): linux/amd64, linux/arm64 -> ${IMAGE}:${TAG}"
    fi
    docker "${args[@]}" .
else
    echo "构建镜像: ${IMAGE}:${TAG}"
    docker build -t "${IMAGE}:${TAG}" .

    if [ -n "$PUSH" ]; then
        echo "推送到 Docker Hub: ${IMAGE}:${TAG}"
        docker push "${IMAGE}:${TAG}"
    fi
fi

echo ""
if [ -n "$PUSH" ]; then
    echo "✅ 已完成并推送: ${IMAGE}:${TAG}"
    echo "   拉取: docker pull ${IMAGE}:${TAG}"
else
    echo "✅ 已本地构建: ${IMAGE}:${TAG}（未推送）"
    echo "   本地试运行: docker run --rm ${IMAGE}:${TAG} --help"
fi
