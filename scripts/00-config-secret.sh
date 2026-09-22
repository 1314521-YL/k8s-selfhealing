#!/bin/bash
# 00: 将 config.env 中的 SMTP/LLM 配置注入 K8s Secret (heal-agent 使用)
set -e
cd "$(dirname "$0")/.."
kubectl create namespace monitoring --dry-run=client -o yaml | kubectl apply -f -
kubectl -n monitoring delete secret heal-agent-config --ignore-not-found
kubectl -n monitoring create secret generic heal-agent-config --from-env-file=config.env
echo "✓ heal-agent-config Secret 已创建 (修改 config.env 后重跑本脚本并重启 heal-agent)"
