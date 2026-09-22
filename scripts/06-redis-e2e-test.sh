#!/bin/bash
# 06: Redis 自愈链路端到端验证
#   MODE=crash   (默认): 连续崩溃 3 次 -> CrashLoop 告警 -> 自动重启 -> 恢复闭环
#   MODE=memfill        : DEBUG POPULATE 打满内存 -> MemoryHigh 告警 -> 重启清缓存恢复
#   MODE=clean          : 清空数据恢复健康
# 用法: MODE=memfill bash scripts/06-redis-e2e-test.sh
set -e
cd "$(dirname "$0")/.."
MODE=${MODE:-crash}
R="kubectl -n demo exec redis-0 -c redis -- redis-cli"
PROM_RAW="/api/v1/namespaces/monitoring/services/http:kps-prometheus:9090/proxy/api/v1"
HEAL_RAW="/api/v1/namespaces/monitoring/services/http:heal-agent:8080/proxy"

START_TS=$(date -u +%Y-%m-%dT%H:%M:%S)
echo "===== Redis 自愈 E2E (模式: $MODE, 注入时间 $START_TS) ====="

case $MODE in
  crash)
    echo "--> 注入 8 次进程崩溃 (DEBUG SEGFAULT), 期间部分命中退避窗口属正常..."
    for i in $(seq 1 8); do
      kubectl -n demo exec redis-0 -c redis -- redis-cli DEBUG SEGFAULT >/dev/null 2>&1 || true
      R=$(kubectl -n demo get pod redis-0 -o jsonpath='{.status.containerStatuses[?(@.name=="redis")].restartCount}')
      echo "    第 $i 次注入完成, 当前累计重启: $R"
      sleep 15
    done
    EXPECT=RedisPodCrashLooping ;;
  memfill)
    echo "--> 注入 20 万个 key 打满 maxmemory(100MB)..."
    kubectl -n demo exec redis-0 -c redis -- redis-cli DEBUG POPULATE 200000 key 800 >/dev/null
    kubectl -n demo exec redis-0 -c redis -- redis-cli INFO memory | grep -E "used_memory_human|maxmemory_human"
    EXPECT=RedisMemoryHigh ;;
  clean)
    kubectl -n demo exec redis-0 -c redis -- redis-cli FLUSHALL
    echo "✓ Redis 数据已清空, 恢复健康"; exit 0 ;;
  *) echo "MODE 取值: crash | memfill | clean"; exit 1 ;;
esac

echo "--> 等待告警 $EXPECT 触发 (最长 240s)..."
FIRING=""
for i in $(seq 1 48); do
  FIRING=$(kubectl get --raw "$PROM_RAW/alerts" | python3 -c "
import json,sys
for a in json.load(sys.stdin)['data']['alerts']:
    if a['labels'].get('alertname')=='$EXPECT' and a['state']=='firing':
        print(a['labels']['alertname']); break" 2>/dev/null || true)
  [ -n "$FIRING" ] && break
  sleep 5
done
[ -z "$FIRING" ] && { echo "✗ 超时未见 $EXPECT firing"; exit 1; }
echo "✓ 告警已 firing: $FIRING"

echo "--> 等待自愈闭环 (期望 result=recovered, 最长 420s)..."
HISTORY=""
for i in $(seq 1 84); do
  HISTORY=$(kubectl get --raw "$HEAL_RAW/history?limit=10" 2>/dev/null | python3 -c "
import json,sys
try:
    d=json.load(sys.stdin)
    for r in d['records']:
        if r['result']=='recovered' and r['alertname']=='$EXPECT' and r['ts'] >= '$START_TS':
            print(json.dumps(r, ensure_ascii=False)); break
except Exception: pass" 2>/dev/null || true)
  [ -n "$HISTORY" ] && break
  sleep 5
done
[ -z "$HISTORY" ] && { echo "✗ 超时未看到本轮 recovered 记录"; exit 1; }
echo "✓ 闭环记录: $HISTORY"

echo "--> Redis 当前状态:"
kubectl -n demo get pods -l app=redis
echo "===== Redis 自愈 E2E 通过 ($MODE) ====="
echo "提示: memfill 模式重启后缓存已清空; 如需手动清理: MODE=clean bash $0"
