# 监控自愈系统部署文档（零基础版）

> 目标读者：没接触过 Kubernetes / n8n / Prometheus 的新手。
> 按本文从上到下逐步执行，即可在**一台云服务器**上部署完整的
> 「Prometheus 告警 → n8n 自动处置 → 邮件通知 → AI 诊断 → 失败升级人工」自愈系统。
>
> 全程约 30~40 分钟（大部分时间在等镜像下载）。

---

## 0. 你将部署什么

```
┌────────────┐  告警   ┌──────────────┐  webhook  ┌──────────────────────────┐
│ Prometheus │───────▶│ Alertmanager │──────────▶│ n8n (统一告警入口)        │
└────────────┘        └──────────────┘           └────┬─────────────────────┘
                                              ┌───────┼──────────────┐
                                              ▼       ▼              ▼
                                          ①邮件通知  ②自愈重启    ③AI 诊断
                                              └───────┼──────────────┘
                                                      ▼
                                          heal-agent（执行器）
                                          恢复验证 → 成功回写闭环 / 失败升级人工
```

| 组件 | 作用 | 访问方式（部署完成后） |
|---|---|---|
| k3s | 轻量 Kubernetes | — |
| Prometheus + Alertmanager | 采集指标、触发告警 | 集群内部 |
| n8n | 告警处置流程编排 | `http://服务器IP:30567` |
| Grafana | 监控看板 | `http://服务器IP:30300` |
| heal-agent | 自愈执行/邮件/AI诊断/历史记录 | 集群内部 |
| demo-api | 演练用的"会生病"的应用 | `http://服务器IP:30081` |
| Redis + exporter | 演练用的缓存服务（含指标采集） | 集群内 `redis.demo:6379` |

**服务器要求**

| 项目 | 最低 | 推荐 |
|---|---|---|
| CPU | 2 核 | 2 核 |
| 内存 | 4 GB | 8 GB |
| 磁盘 | 40 GB | 60 GB+ |
| 系统 | Ubuntu 22.04 / 24.04（Debian 亦可） | Ubuntu 24.04 |
| 网络 | 能访问外网（拉取镜像/图表） | — |
| 权限 | root 或 sudo | root |

> 防火墙/安全组放行端口：`6443, 30567, 30300, 30081`（后三个是 Web 访问和演示用）。

---

## 1. 安装 k3s（约 3 分钟）

以 root 登录服务器，执行：

```bash
curl -sfL https://rancher.com/mirror-k3s/k3s-installation/install.sh | sh -
```

> 国内网络若拉取缓慢，改用：
> ```bash
> curl -sfL https://rancher-mirror.rancher.cn/k3s/k3s-install.sh | INSTALL_K3S_MIRROR=cn sh -
> ```

验证（看到 `Ready` 即成功）：

```bash
kubectl get nodes
```

预期输出：

```
NAME              STATUS   ROLES                  AGE   VERSION
your-server-name  Ready    control-plane,master   1m    v1.3x.x+k3s1
```

---

## 2. 安装 Docker（约 2 分钟）

用于构建 heal-agent 和演示应用的镜像：

```bash
curl -fsSL https://get.docker.com | sh
docker --version    # 能打印版本号即可
```

---

## 3. 安装 Helm（约 1 分钟）

```bash
mkdir -p /root/bin
curl -fsSL https://get.helm.sh/helm-v3.16.4-linux-amd64.tar.gz -o /tmp/helm.tgz
tar xzf /tmp/helm.tgz -C /tmp
install /tmp/linux-amd64/helm /root/bin/helm
/root/bin/helm version --short    # 预期: v3.16.4+g7877b45
```

> ⚠️ 本文所有 `helm` 命令都写的是 `/root/bin/helm`，**直接敲 `helm` 会提示命令不存在**，
> 因为装在了自定义目录（不影响使用）。

---

## 4. 获取项目文件

把项目目录 `k8s-selfhealing/` 拷贝到服务器的 `/root/` 下。
如果你是从别的机器拷贝（在本机执行，不是在服务器上）：

```bash
scp -r k8s-selfhealing root@服务器IP:/root/
```

确认文件齐全：

```bash
ls /root/k8s-selfhealing
# 预期: README.md  config.env  demo-app  heal-agent  monitoring  n8n  scripts
```

各目录说明：

| 路径 | 内容 |
|---|---|
| `config.env` | **邮件 + AI Key 的配置文件（唯一需要你修改的文件）** |
| `monitoring/` | Prometheus 套件的 helm 配置 |
| `heal-agent/` | 自愈执行器（代码 + 部署清单） |
| `n8n/` | n8n 部署清单 + 告警处置工作流 |
| `demo-app/` | 演练应用 + 告警规则 |
| `scripts/` | 一键部署/测试脚本 |

---

## 5. 部署（核心步骤，按顺序执行）

### 5.1 先跑通链路（邮件/AI 稍后再配）

> 配置文件留空也能部署——系统会自动降级：邮件内容打印到日志、AI 用内置规则模板。
> 建议新手先这样跑通全链路，再回过头填配置。

创建配置 Secret：

```bash
cd /root/k8s-selfhealing
bash scripts/00-config-secret.sh
```

预期输出：

```
secret/heal-agent-config created
✓ heal-agent-config Secret 已创建
```

### 5.2 部署监控栈（约 5~10 分钟，取决于网络）

```bash
/root/bin/helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
KUBECONFIG=/etc/rancher/k3s/k3s.yaml /root/bin/helm upgrade --install kps \
  prometheus-community/kube-prometheus-stack -n monitoring --create-namespace \
  -f /root/k8s-selfhealing/monitoring/kube-prometheus-values.yaml --timeout 15m
```

> ⚠️ 两个新手常错点：
> 1. `helm` 前必须带 `KUBECONFIG=/etc/rancher/k3s/k3s.yaml`，否则报
>    `Kubernetes cluster unreachable: ... localhost:8080`。
> 2. 命令很长，注意换行符 `\` 后不能有空格。

验证（所有 Pod 都是 `Running` 即成功，约需 2~3 分钟）：

```bash
kubectl -n monitoring get pods
```

预期看到（`alertmanager-kps-alertmanager-0` 一定要存在！）：

```
alertmanager-kps-alertmanager-0            2/2     Running
kps-grafana-xxxx                           3/3     Running
kps-kube-state-metrics-xxxx                1/1     Running
kps-operator-xxxx                          1/1     Running
kps-prometheus-node-exporter-xxxx          1/1     Running
prometheus-kps-prometheus-0                2/2     Running
```

> ❗ 如果**没有** alertmanager 的 Pod：说明/values 里自定义配置有问题，
> 查看原因：`kubectl -n monitoring logs deploy/kps-operator | grep -i error`
> 常见报错 `undefined receiver "null"` → 确认 values 的 receivers 里有 `- name: "null"`（本项目 values 已带，勿删）。

### 5.3 部署 heal-agent（约 3 分钟）

```bash
bash /root/k8s-selfhealing/scripts/01-deploy-heal-agent.sh
```

脚本自动完成：docker 构建镜像 → 导入 k3s → 部署 → 等待就绪。
预期最后输出 `✓ heal-agent 就绪`。

验证：

```bash
kubectl -n monitoring get pods -l app=heal-agent
# STATUS 应为 Running
```

### 5.4 部署 n8n + 告警工作流（约 5 分钟）

```bash
bash /root/k8s-selfhealing/scripts/02-deploy-n8n.sh
```

脚本自动完成：部署 n8n → 通过 API 导入并激活工作流 → 重启注册 Webhook。
预期最后输出 `✓ n8n 就绪: http://<NODE_IP>:30567`。

验证：

```bash
# ① Pod 正常
kubectl -n monitoring get pods -l app=n8n
# ② 工作流存在且激活（看到 jvRB...|Alertmanager 自愈闭环处置 即可）
POD=$(kubectl -n monitoring get pod -l app=n8n -o jsonpath='{.items[0].metadata.name}')
kubectl -n monitoring exec $POD -- n8n list:workflow 2>/dev/null | grep 自愈
```

浏览器打开 `http://服务器IP:30567`，用 `admin@selfheal.local / Selfheal#2026` 登录
（**登录后请立即改密码**：右上角头像 → Settings → Personal）。

### 5.5 部署演练应用 + 告警规则（约 3 分钟）

```bash
bash /root/k8s-selfhealing/scripts/03-deploy-demo.sh
```

预期输出 `✓ demo-api 就绪 (NodePort 30081)`。

验证指标已被 Prometheus 抓取（值应为 `"0"`）：

```bash
kubectl get --raw "/api/v1/namespaces/monitoring/services/http:kps-prometheus:9090/proxy/api/v1/query?query=demo_api_fail_mode" | grep '"0"'
```

### 5.6 部署 Redis（可选，约 3 分钟，推荐）

部署一个演示 Redis（StatefulSet 单实例 + redis-exporter 指标采集边车），
它的告警规则会**自动接入同一套 n8n 自愈链路**——演示如何把任意中间件纳入监控自愈体系：

```bash
bash /root/k8s-selfhealing/scripts/05-deploy-redis.sh
```

预期输出 `✓ Redis 就绪`，且 Pod 为 `2/2 Running`（redis + exporter 两个容器）：

```bash
kubectl -n demo get pods -l app=redis
# NAME      READY   STATUS    RESTARTS   AGE
# redis-0   2/2     Running   0          30s
```

验证指标已抓取（等待约 1 分钟后执行，值应为 `'1'`）：

```bash
kubectl get --raw "/api/v1/namespaces/monitoring/services/http:kps-prometheus:9090/proxy/api/v1/query?query=redis_up"
# 返回的 JSON 中 "value": [..., "1"] 即正常
```

> 该 Redis 为缓存型演示实例：无持久化、maxmemory 100MB、注入调试命令已开启。
> 生产环境请勿照抄这些参数（尤其持久化与 DEBUG 命令开关）。

---

## 6. 验收：跑一次完整的自愈闭环

```bash
bash /root/k8s-selfhealing/scripts/04-e2e-test.sh
```

**脚本会自动做的事**：向演示应用注入内存泄漏故障 → 等告警触发 → 等 n8n 自动重启应用 → 等闭环记录出现。

预期输出（全程约 2~4 分钟）：

```
===== E2E 测试 (模式: volatile) =====
--> 注入可自愈故障
故障已注入: 内存泄漏, 模式=volatile(重启可恢复)
--> 等待告警触发 (Prometheus, 最长 150s)...
✓ 告警已 firing: DemoAppFailMode
--> 等待自愈链路产出结果 (期望 result=recovered, 最长 600s)...
✓ 闭环记录: {"id": 67, "result": "recovered", "attempts": 1, ...}
===== E2E 测试通过 (volatile -> recovered) =====
```

**三种模式**：

```bash
bash scripts/04-e2e-test.sh                  # 场景A: 故障可自愈 → 自动重启恢复
MODE=sticky bash scripts/04-e2e-test.sh      # 场景B: 故障重启无效 → 升级人工P1
MODE=clear bash scripts/04-e2e-test.sh       # 清除故障标记
```

查看处置历史（含 MTTR 统计）：

```bash
kubectl get --raw "/api/v1/namespaces/monitoring/services/http:heal-agent:8080/proxy/history?limit=10" | python3 -m json.tool
```

### Redis 故障演练（进阶，需先完成 5.6）

Redis 的 3 条告警规则已自动接入自愈链路：

| 告警 | 触发条件 | 自愈动作 |
|---|---|---|
| `RedisPodCrashLooping` | 近 5 分钟重启 >2 次且当前未就绪 | 自动滚动重启 |
| `RedisMemoryHigh` | 内存超过 maxmemory 的 90% | 滚动重启清空缓存（缓存类实例） |
| `RedisDown` | redis_up=0 持续 2 分钟 | 滚动重启，无效则升级人工 |

**演练一：内存打满（推荐，结果完全确定，约 3~5 分钟）**

```bash
MODE=memfill bash /root/k8s-selfhealing/scripts/06-redis-e2e-test.sh
```

脚本会向 Redis 写入 20 万个 key 把内存塞满（实测约 145MB/100MB），预期输出：

```
✓ 告警已 firing: RedisMemoryHigh
✓ 闭环记录: {"result": "recovered", "engine": "llm", "attempts": 1, "duration_seconds": 94.2, ...}
===== Redis 自愈 E2E 通过 (memfill) =====
```

原理：自愈滚动重启 → 新实例缓存为空 → 内存占比回落到 ~1% → 告警解除 → 闭环邮件。

**演练二：进程反复崩溃（约 4~6 分钟）**

```bash
MODE=crash bash /root/k8s-selfhealing/scripts/06-redis-e2e-test.sh
```

脚本连续注入 8 次进程崩溃（部分会命中 k8s 退避窗口被忽略，属正常），
累计约 3 次有效重启后触发 `RedisPodCrashLooping`，预期同样以 `recovered` 闭环结束。

**演练三（可选）：验证"重启无效→升级人工"路径**

给 Redis 挂载坏配置或直接删掉 StatefulSet 的RBAC权限等制造"重启救不了"的故障较繁琐，
用 demo-api 的 sticky 模式体验即可（见上文场景B），行为完全一致：
自动重启 ×2 → 验证仍失败 → `[P1][需人工介入]` 高优先级邮件。

**演练后清理**：

```bash
MODE=clean bash /root/k8s-selfhealing/scripts/06-redis-e2e-test.sh   # 清空 Redis 数据
```

> 📧 配置过邮件后，每次演练邮箱会收到对应的 `[告警受理]`、`[已自愈]`、`[已恢复]` 系列邮件，
> AI 诊断内容会具体到崩溃原因（如 SIGSEGV 退出码 139）与处置建议。

---

## 7. 配置邮件通知 + AI 诊断（让系统"开口说话"）

### 7.1 编辑配置文件

```bash
vim /root/k8s-selfhealing/config.env
```

**邮件（以 QQ 邮箱为例）**：

```ini
SMTP_HOST=smtp.qq.com
SMTP_PORT=465
SMTP_USER=你的QQ号@qq.com        # 例如 123456@qq.com
SMTP_PASS=16位授权码             # 不是QQ密码! 获取方式见下
SMTP_FROM=你的QQ号@qq.com        # 与 SMTP_USER 相同
SMTP_TO=接收告警的邮箱            # 可以填自己, 多个用英文逗号分隔
SMTP_STARTTLS=false             # 465端口填 false
```

**授权码获取**：登录 [mail.qq.com](https://mail.qq.com) → 设置 → 账号 →
开启「IMAP/SMTP 服务」→ 手机短信验证 → 得到 16 位授权码。

**AI 诊断（OpenAI 兼容接口，任选一家）**：

```ini
# DeepSeek 示例
OPENAI_BASE_URL=https://api.deepseek.com/v1
OPENAI_API_KEY=sk-xxxx
OPENAI_MODEL=deepseek-chat
```

> ⚠️ **新手最容易错的两点**（都是实测踩过的坑）：
> 1. `OPENAI_BASE_URL` 结尾**必须带 `/v1`**（多数中转/代理都如此），
>    缺了会返回网页导致诊断降级。
> 2. 部分 API 代理/防火墙会拦截程序默认请求头，如遇 403，
>    本项目 heal-agent 已内置处理（v1.2+），无需额外操作。

### 7.2 使配置生效

```bash
cd /root/k8s-selfhealing
bash scripts/00-config-secret.sh
kubectl -n monitoring rollout restart deploy/heal-agent
kubectl -n monitoring rollout status deploy/heal-agent --timeout=120s
```

### 7.3 分别验证邮件与 AI

```bash
# 验证 AI 诊断（engine 应为 "llm"）
kubectl -n monitoring exec deploy/heal-agent -- wget -qO- --timeout=120 \
  --post-data='{"alert":{"alertname":"TestCheck","namespace":"demo","kind":"deployment","name":"demo-api"}}' \
  http://127.0.0.1:8080/ai/diagnose
# 预期: {"engine": "llm", "diagnosis": "## 根因分析..."}

# 验证邮件（sent 应为 true, 然后去收件箱查收）
kubectl -n monitoring exec deploy/heal-agent -- wget -qO- \
  --post-data='{"subject":"[自愈系统] 测试邮件","body":"配置成功"}' \
  http://127.0.0.1:8080/notify/email
# 预期: {"ok": true, "sent": true}
```

### 7.4 完整链路复验（可选但推荐）

```bash
bash scripts/04-e2e-test.sh
```

再跑一遍，这次邮箱会真实收到 3 封邮件：
`[告警受理]`（高优先级）→ `[已自愈]`（含 AI 诊断全文）→ `[已恢复]`（闭环回执）。

---

## 8. 日常使用速查

| 想做什么 | 命令/入口 |
|---|---|
| 注入演示故障 | `curl -X POST http://服务器IP:30081/fail` |
| 注入"重启无效"故障 | `curl -X POST "http://服务器IP:30081/fail?sticky=1"` |
| 清除故障 | `curl -X POST http://服务器IP:30081/ok` |
| Redis 崩溃演练 / 内存演练 / 清理 | `MODE=crash\|memfill\|clean bash scripts/06-redis-e2e-test.sh` |
| 手动执行 Redis 命令 | `kubectl -n demo exec redis-0 -c redis -- redis-cli <命令>` |
| 看处置历史/MTTR | `kubectl get --raw "/api/v1/namespaces/monitoring/services/http:heal-agent:8080/proxy/history?limit=20" \| python3 -m json.tool` |
| 看 n8n 每次执行轨迹 | 浏览器 `http://服务器IP:30567` → Executions |
| 看监控大盘 | 浏览器 `http://服务器IP:30300`（admin / admin123） |
| 看 heal-agent 日志（含降级邮件内容） | `kubectl -n monitoring logs deploy/heal-agent --tail=50` |
| 修改工作流后重新生效 | 编辑 `n8n/workflow-alert-handler.json` → 重跑 `scripts/02-deploy-n8n.sh` |

**接入你自己的业务告警**：在 PrometheusRule 的 labels 里声明四要素即可被自动处置：

```yaml
labels:
  severity: critical
  namespace: 业务命名空间
  kind: deployment        # deployment | statefulset | pod
  name: 应用名
  heal_strategy: restart  # restart=自动重启; none=仅通知+AI诊断
```

> 💡 `redis/deploy.yaml` 就是"接入一个中间件"的完整参照模板：
> Workload（StatefulSet/Deployment）+ exporter 边车 + Service + ServiceMonitor + PrometheusRule
> 五件套，照葫芦画瓢即可接入 MySQL、Kafka、Nginx 等（换对应 exporter 镜像与告警表达式即可）。

---

## 9. 常见问题排查（新手必读）

| 症状 | 原因 | 解决 |
|---|---|---|
| `helm` 报 `Kubernetes cluster unreachable ... localhost:8080` | 没带 KUBECONFIG | 命令前加 `KUBECONFIG=/etc/rancher/k3s/k3s.yaml` |
| 没有 alertmanager Pod，operator 日志报 `undefined receiver "null"` | values 里删掉了空接收器 | 恢复 values 中 `- name: "null"`，重新 helm upgrade |
| `05/04-e2e-test` 报 150s 内无告警 | demo 指标未被抓取 | 检查 ServiceMonitor：Service 必须带 `app: demo-api` 标签（本项目清单已带，勿改） |
| 镜像 `ImagePullBackOff` | 本地镜像没导入成功 | 重跑对应 `scripts/0x-deploy-*.sh`（内含 build+import） |
| n8n 页面 502/打不开 | Pod 未就绪 | `kubectl -n monitoring get pods -l app=n8n`，等待或看 `kubectl logs` |
| 邮件收不到 | ① 未配置 ② 授权码错 ③ 进了垃圾箱 | ① 看 heal-agent 日志是否提示 `smtp_not_configured` ② 重新生成授权码 ③ 检查垃圾邮件并标记非垃圾 |
| AI 诊断始终是 rules 模板 | ① Key 未生效 ② BASE_URL 缺 `/v1` ③ 接口报错 | ① 重跑 7.2 ② 补 `/v1` ③ `kubectl -n monitoring logs deploy/heal-agent \| grep "\[ai\]"` 看具体错误 |
| 工作流执行了但没动作 | 表达式错误 | n8n UI → Executions → 点开失败节点看报错 |
| Redis 指标抓不到，target 报 `400 Bad Request` | exporter 抓取路径配错（`/scrape` 是多目标模式，需带参数） | ServiceMonitor 的 `path` 必须为 `/metrics`（本项目清单已正确，勿改） |
| Redis 崩溃自愈**成功**了却收到"升级人工"邮件 | CrashLoop 告警裸用 `increase(restarts[5m])>2`：Pod 恢复（计数器重置）后该值在滑动窗口内残留约 5 分钟 | 表达式需叠加 `and kube_pod_container_status_ready == 0` 让告警恢复即解除（本项目规则已内置正确写法） |
| Redis 故障注入后没反应 | 注入命中了 k8s 崩溃退避窗口，exec 被忽略 | 属正常现象，脚本已自动注入 8 次（约 3 次有效）；也可改用 `MODE=memfill`（结果确定） |
| Pod 反复重启 `CrashLoopBackOff` | 看 lastState 原因 | `kubectl -n <ns> describe pod <pod名>` |

**万能排查三连**：

```bash
kubectl get pods -A                                   # ① 谁不正常
kubectl -n <命名空间> logs <Pod名> --tail=50           # ② 它说了什么
kubectl -n <命名空间> describe pod <Pod名>             # ③ 事件里发生了什么
```

---

## 10. 卸载（如需清理）

```bash
KUBECONFIG=/etc/rancher/k3s/k3s.yaml /root/bin/helm uninstall kps -n monitoring
kubectl delete namespace monitoring demo
# 如需连 k3s 一起卸载:
/usr/local/bin/k3s-uninstall.sh
```

---

## 附：部署完成的最终形态

```bash
kubectl get pods -A | grep -E "monitoring|demo"
```

```
monitoring   alertmanager-kps-alertmanager-0    2/2  Running
monitoring   heal-agent-xxxx                    1/1  Running
monitoring   kps-grafana-xxxx                   3/3  Running
monitoring   kps-kube-state-metrics-xxxx        1/1  Running
monitoring   kps-operator-xxxx                  1/1  Running
monitoring   kps-prometheus-node-exporter-xxxx  1/1  Running
monitoring   n8n-xxxx                           1/1  Running
monitoring   prometheus-kps-prometheus-0        2/2  Running
demo         demo-api-xxxx                      1/1  Running
```

全部 `Running`，然后回到第 6 节做验收即可。祝部署顺利！
