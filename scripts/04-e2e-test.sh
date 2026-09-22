#!/bin/bash
# 04: 端到端验证
#   MODE=volatile (默认): 注入可自愈故障 -> 通知 -> 重启 -> 验证 -> 闭环通知
#   MODE=sticky :         注入持久化故障 -> 重启无效 -> 升级人工
#   用法: MODE=sticky bash scripts/04-e2e-test.sh
set -e
cd "$(dirname "$0")/.."
MODE=${MODE:-volatile}
NODE_IP=$(kubectl get node -o jsonpath='{.items[0].status.addresses[?(@.type=="InternalIP")].address}')
DEMO_URL="http://$NODE_IP:30081"
PROM_RAW="/api/v1/namespaces/monitoring/services/http:kps-prometheus:9090/proxy/api/v1"
HEAL_RAW="/api/v1/namespaces/monitoring/services/http:heal-agent:8080/proxy"

echo "===== E2E 测试 (模式: $MODE) ====="
echo "目标: $DEMO_URL"

case $MODE in
  volatile) echo "--> 注入可自愈故障";  curl -s -X POST "$DEMO_URL/fail" ;;
  sticky)   echo "--> 注入持久化故障";  curl -s -X POST "$DEMO_URL/fail?sticky=1" ;;
  clear)    echo "--> 清除故障";        curl -s -X POST "$DEMO_URL/ok"; exit 0 ;;
  *) echo "MODE 取值: volatile|sticky|clear"; exit 1 ;;
esac

echo "--> 等待告警触发 (Prometheus, 最长 150s)..."
FIRING=""
for i in $(seq 1 30); do
  FIRING=$(kubectl get --raw "$PROM_RAW/alerts" | python3 -c "
import json,sys
d=json.load(sys.stdin)
for a in d['data']['alerts']:
    if a['labels'].get('alertname','').startswith('DemoApp') and a['state']=='firing':
        print(a['labels']['alertname'], end=' ')
" 2>/dev/null || true)
  [ -n "$FIRING" ] && break
  sleep 5
done
[ -z "$FIRING" ] && { echo "✗ 150s 内未见 DemoApp* 告警 firing"; exit 1; }
echo "✓ 告警已 firing: $FIRING"

EXPECT=recovered; [ "$MODE" = "sticky" ] && EXPECT=escalated
echo "--> 等待自愈链路产出结果 (期望 result=$EXPECT, 最长 600s)..."
HISTORY=""
for i in $(seq 1 120); do
  HISTORY=$(kubectl get --raw "$HEAL_RAW/history?limit=5" 2>/dev/null | python3 -c "
import json,sys
try:
    d=json.load(sys.stdin)
    for r in d['records']:
        if r['result']=='$EXPECT' and r['alertname'].startswith('DemoApp'):
            print(json.dumps(r, ensure_ascii=False)); break
except Exception: pass" 2>/dev/null || true)
  [ -n "$HISTORY" ] && break
  sleep 5
done
[ -z "$HISTORY" ] && { echo "✗ 超时未看到 result=$EXPECT 记录"; kubectl get --raw "$HEAL_RAW/history?limit=10"; exit 1; }
echo "✓ 闭环记录: $HISTORY"

echo "--> 当前 demo-api Pod 状态:"
kubectl -n demo get pods -l app=demo-api
echo "--> heal-agent 历史摘要:"
kubectl get --raw "$HEAL_RAW/history?limit=10" | python3 -m json.tool | tail -20
echo "===== E2E 测试通过 ($MODE -> $EXPECT) ====="
