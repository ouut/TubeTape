#!/usr/bin/env bash
# 构建 TubeTape Docker 镜像
#
# 用法：
#   ./scripts/docker_build.sh                 # 构建 tubetape:latest（当前架构）
#   ./scripts/docker_build.sh v0.1.1          # 指定 tag
#   ./scripts/docker_build.sh v0.1.1 --push   # 构建并推送（单架构）
#   ./scripts/docker_build.sh latest --multi  # 多架构构建（amd64 + arm64，需 buildx）
set -euo pipefail

IMAGE="tubetape"
TAG="${1:-latest}"
PUSH=""
MULTI=""

for arg in "${@:2}"; do
    case "$arg" in
        --push) PUSH="--push" ;;
        --multi) MULTI="1" ;;
        *) echo "未知参数: $arg" >&2; exit 2 ;;
    esac
done

cd "$(dirname "$0")/.."

if [ -n "$MULTI" ]; then
    echo "多架构构建: linux/amd64, linux/arm64"
    docker buildx build \
        --platform linux/amd64,linux/arm64 \
        -t "${IMAGE}:${TAG}" \
        $PUSH \
        .
else
    docker build -t "${IMAGE}:${TAG}" .
fi

echo ""
echo "构建完成: ${IMAGE}:${TAG}"
echo "运行示例见 README，或："
echo "  docker run --rm ${IMAGE}:${TAG} --help"
