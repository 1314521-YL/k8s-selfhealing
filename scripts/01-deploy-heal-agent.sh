#!/bin/bash
# 01: 构建 heal-agent 镜像并部署 (含 RBAC 最小权限)
set -e
cd "$(dirname "$0")/.."
bash scripts/build-and-import.sh heal-agent:1.0 heal-agent
kubectl apply -f heal-agent/rbac.yaml
kubectl apply -f heal-agent/deploy.yaml
kubectl -n monitoring rollout status deploy/heal-agent --timeout=180s
echo "✓ heal-agent 就绪"
