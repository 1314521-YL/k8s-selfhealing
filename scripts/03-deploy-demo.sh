#!/bin/bash
# 03: 构建并部署 demo-app (演示故障注入目标 + 告警规则 + ServiceMonitor)
set -e
cd "$(dirname "$0")/.."
bash scripts/build-and-import.sh demo-api:1.0 demo-app
kubectl apply -f demo-app/deploy.yaml
kubectl -n demo rollout status deploy/demo-api --timeout=180s
echo "✓ demo-api 就绪 (NodePort 30081)"
