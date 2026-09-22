#!/bin/bash
# 通用: docker 构建镜像并导入 k3s containerd
# 用法: ./build-and-import.sh <镜像名:tag> <构建目录>
set -e
IMG=$1
DIR=$2
docker build -t "$IMG" "$DIR"
docker save "$IMG" | sudo k3s ctr images import -
sudo k3s ctr -n k8s.io images ls | grep "$(echo "$IMG" | cut -d: -f1)" || true
echo "✓ $IMG 已导入 k3s"
