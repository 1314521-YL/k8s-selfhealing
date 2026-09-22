#!/bin/bash
# 05: 部署演示 Redis + exporter + 告警规则(接入自愈链路)
set -e
cd "$(dirname "$0")/.."
kubectl apply -f redis/deploy.yaml
kubectl -n demo rollout status statefulset/redis --timeout=300s
echo "--> Redis 与 exporter 状态:"
kubectl -n demo get pods -l app=redis
echo "✓ Redis 就绪"
