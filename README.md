# k3s 云端监控自愈解决方案（n8n 统一告警入口）

> 📖 **新手部署请看 [DEPLOY.md](DEPLOY.md)（零基础逐步部署文档，含排错手册）**，本文为架构与使用速查。

以 **n8n 作为告警统一事件入口**，将 Prometheus / Alertmanager 告警接入
**「通知 → 自愈重启 → AI 诊断」多路自动化处置链路**，处置结果回写形成闭环，
失败自动升级人工，显著缩短故障平均恢复时间（MTTR）。

## 架构

```
┌────────────┐  alerts   ┌──────────────┐  webhook   ┌────────────────────────────┐
│ Prometheus │──────────▶│ Alertmanager │───────────▶│  n8n (统一事件入口, NodePort │
└────────────┘           └──────────────┘            │  30567)  闭环处置工作流      │
      ▲ 15s评估                                      └──────┬─────────────────────┘
      │  ServiceMonitor/PrometheusRule         ┌─────────────┼──────────────┐
      │                                        ▼             ▼              ▼
┌─────┴──────┐                          ①邮件通知     ②自愈重启      ③AI 诊断
│ kube-state- │                                │      (K8s API+RBAC)  (OpenAI兼容/规则降级)
│ metrics     │                                └──────────┬────────────┘
└────────────┘                                           ▼
                                              ┌─────────────────────┐
                             ④恢复验证: 副本健康 │   heal-agent        │
                             + AM告警是否仍燃烧  │  (执行/通知/记录/诊断)│
                                              └──────┬──────────────┘
                                    恢复 ✓ │ 未恢复 ✗ ▼
                        ┌──────────────────────┐   ┌─────────────────────┐
                        │ 闭环回写:             │   │ 重试1次 → 仍未恢复    │
                        │ · 记录 sqlite 历史    │   │ 升级人工:            │
                        │ · 注解工作负载         │   │ · P1 高优邮件        │
                        │ · 恢复确认邮件(含诊断) │   │ · 记录 escalated     │
                        └──────────────────────┘   └─────────────────────┘
```

## 组件与端口

| 组件 | 位置 | 访问 |
|---|---|---|
| n8n（编排） | monitoring | http://<NODE_IP>:30567 （admin@selfheal.local / Selfheal#2026，请尽快修改） |
| Grafana | monitoring | http://<NODE_IP>:30300 （admin / admin123） |
| Prometheus / Alertmanager | monitoring | 集群内 kps-prometheus:9090 / kps-alertmanager:9093 |
| heal-agent（执行器） | monitoring | 集群内 heal-agent.monitoring:8080 |
| demo-api（演练目标） | demo | http://<NODE_IP>:30081 |

## heal-agent（运维执行器，纯标准库零依赖）

n8n 只做编排，所有"动手"能力收敛到 heal-agent，RBAC 最小权限（仅 demo 命名空间）：

| 接口 | 作用 |
|---|---|
| `POST /heal/restart` | 滚动重启 Deployment/Statefulset 或删除 Pod |
| `POST /heal/annotate` | 处置结果回写：注解工作负载（selfheal.io/*） |
| `GET  /verify` | 恢复验证：副本健康 + Alertmanager 告警是否仍燃烧 |
| `GET  /context` | 采集 Pod 状态/重启原因/事件/日志，供 AI 诊断 |
| `POST /ai/diagnose` | LLM 诊断（OpenAI 兼容）；未配 Key 自动降级规则模板 |
| `POST /notify/email` | SMTP 邮件通知；未配置降级为日志输出，不阻塞链路 |
| `POST /record` / `GET /history` | 处置历史（sqlite 持久化）+ MTTR 统计（闭环审计） |

## 快速开始

```bash
# 0) 配置 SMTP / LLM（可留空跑通链路，heal-agent 会自动降级）
vim config.env && bash scripts/00-config-secret.sh

# 1) 监控栈（kube-prometheus-stack 精简版, AM 已路由到 n8n webhook）
KUBECONFIG=/etc/rancher/k3s/k3s.yaml helm upgrade --install kps \
  prometheus-community/kube-prometheus-stack -n monitoring --create-namespace \
  -f monitoring/kube-prometheus-values.yaml

# 2) heal-agent（构建镜像并导入 k3s）
bash scripts/01-deploy-heal-agent.sh

# 3) n8n + 告警闭环处置工作流（自动导入/更新并激活）
bash scripts/02-deploy-n8n.sh

# 4) 演练应用 + 告警规则
bash scripts/03-deploy-demo.sh
```

## 演示 MTTR 闭环

```bash
# 场景A: 可自愈故障（内存泄漏，重启即恢复）
bash scripts/04-e2e-test.sh                 # 期望: firing → restart → recovered (约2分钟)

# 场景B: 持久化故障（PVC 标记，重启无效 → 升级人工 P1）
MODE=sticky bash scripts/04-e2e-test.sh     # 期望: firing → restart x2 → escalated

# 清除故障
MODE=clear bash scripts/04-e2e-test.sh

# 查看处置历史与 MTTR
kubectl get --raw "/api/v1/namespaces/monitoring/services/http:heal-agent:8080/proxy/history?limit=20" | python3 -m json.tool
```

链路时延预算（volatile 场景实测约 66s 端到端处置）：
故障注入 → 指标暴露(≤15s) → 规则评估触发(≤15s) → AM group_wait(5s) →
n8n 受理通知 + 滚动重启(秒级) → AI 诊断(并行恢复窗口) → 60s 验证 → 闭环回写。

## 告警接入约定（扩展到业务告警）

在 PrometheusRule 的 `labels` 中声明自愈语义，n8n 即可自动处置：

```yaml
- alert: YourAppCrashLooping
  expr: increase(kube_pod_container_status_restarts_total{namespace="prod",pod=~"your-app-.*"}[5m]) > 2
  labels:
    severity: critical
    namespace: prod          # 处置目标所在命名空间
    kind: deployment         # deployment | statefulset | pod
    name: your-app           # 处置目标名称
    heal_strategy: restart   # restart=自动重启 | none=仅通知+AI诊断
```

`heal_strategy=none`（如 K8sNodeNotReady）走"通知 + AI 诊断 + 记录"分支，不执行重启。

## 配置项（config.env → Secret → heal-agent）

| 变量 | 说明 |
|---|---|
| `SMTP_HOST/PORT/USER/PASS/FROM/TO` | 邮件通知；HOST/TO 为空时降级日志（kubectl logs heal-agent 可见邮件内容） |
| `SMTP_STARTTLS` | true=587 STARTTLS；false=465 SSL |
| `OPENAI_BASE_URL/API_KEY/MODEL` | OpenAI 兼容接口（DeepSeek/通义/Kimi…）；无 Key 自动降级规则模板 |

修改后重跑 `00-config-secret.sh` 并 `kubectl -n monitoring rollout restart deploy/heal-agent`。

## 验证 AI 诊断

填入 Key 后无需改工作流：`POST /ai/diagnose` 返回 `engine: llm`，诊断内容自动
进入邮件与历史记录。查看 AI 输出：

```bash
kubectl -n monitoring logs deploy/heal-agent | grep -A5 "\[ai\]"
```

## Redis 监控自愈（已内置）

`redis/` 目录提供单实例演示 Redis（StatefulSet + redis-exporter 边车），告警规则已接入自愈链路：

| 告警 | 条件 | 自愈策略 |
|---|---|---|
| RedisPodCrashLooping | 近5分钟重启>2 且当前未 Ready | 自动滚动重启 |
| RedisMemoryHigh | 内存 > maxmemory 90% | 滚动重启清缓存（缓存类） |
| RedisDown | redis_up=0 持续 2 分钟 | 滚动重启，无效则升级人工 |

```bash
bash scripts/05-deploy-redis.sh                        # 部署
MODE=crash   bash scripts/06-redis-e2e-test.sh         # 注入进程崩溃(4次有效) -> 自动重启闭环
MODE=memfill bash scripts/06-redis-e2e-test.sh         # 注入20万key打满内存 -> 重启清缓存闭环
MODE=clean   bash scripts/06-redis-e2e-test.sh         # 清空数据
```

> ⚠️ 告警表达式要点：CrashLoop 检测不要裸用 `increase(restarts[5m])>2`——计数器重置后
> 该值在滑动窗口内残留约 5 分钟，会导致"自愈成功却误判失败而升级"。
> 需叠加 `and kube_pod_container_status_ready == 0` 让告警在恢复后立即解除。
>
> 生产 Redis 高可用应使用 Sentinel/Cluster 自身故障转移；本链路适合单实例缓存或兜底场景。

## 常见运维

- **工作流更新**：编辑 `n8n/workflow-alert-handler.json` 后重跑 `02-deploy-n8n.sh`（按名称 upsert）。
- **n8n 控制台**：NodePort 30567，可查看每次执行的节点级轨迹（Executions）。
- **邮件未收到**：确认 Secret 已含 SMTP 配置；未配置时邮件全文输出在 heal-agent 日志。
- **扩展自愈动作**：在 heal-agent 增加 `/heal/<action>` 端点（如扩容、切流、清理缓存），工作流中仿照 `自愈-重启` 节点调用。

## 已知边界（生产化建议）

- n8n/heal-agent 单副本，建议生产多副本 + n8n 队列模式（Redis）保证高可用。
- 工作流幂等去重依赖 AM `repeat_interval`（3h）；如需更强去重可在 解析告警 节点按 fingerprint 查 /history 近期记录短路。
- heal-agent RBAC 目前限定 demo 命名空间；扩大处置范围时同步扩展 Role。
- 默认告警规则已精简（关闭 etcd/scheduler/controller-manager 等单节点噪音项）。
