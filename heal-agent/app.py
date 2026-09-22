#!/usr/bin/env python3
"""
heal-agent: 监控自愈体系的"执行器"微服务（纯标准库，零依赖）
  - 自愈动作: 重启 Deployment/Statefulset/Pod (K8s API, RBAC 最小权限)
  - 结果回写: 注解工作负载 + sqlite 历史审计 (闭环)
  - 通知:     SMTP 邮件 (未配置时降级为日志输出, 不阻塞链路)
  - AI 诊断:  OpenAI 兼容 API (未配置 Key 时降级为规则模板)
  - 恢复验证: 工作负载健康 + Alertmanager 告警是否仍在燃烧
"""
import json
import os
import smtplib
import sqlite3
import ssl
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.header import Header
from email.mime.text import MIMEText
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

K8S_API = "https://kubernetes.default.svc"
SA_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
AM_URL = os.environ.get("AM_URL", "http://kps-alertmanager.monitoring.svc.cluster.local:9093")
DB_PATH = os.environ.get("HEAL_DB", "/data/heal-history.db")
PORT = 8080

SSL_CTX = ssl.create_default_context(cafile=os.path.join(SA_DIR, "ca.crt"))
_db_lock = threading.Lock()

# ---------------------------------------------------------------- K8s 客户端

def _sa(name):
    with open(os.path.join(SA_DIR, name)) as f:
        return f.read().strip()

def k8s(method, path, body=None, content_type="application/json", raw=False):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(K8S_API + path, data=data, method=method)
    req.add_header("Authorization", "Bearer " + _sa("token"))
    if data is not None:
        req.add_header("Content-Type", content_type)
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req, context=SSL_CTX, timeout=30) as r:
        payload = r.read().decode()
        if raw:
            return payload
        return json.loads(payload) if payload else {}

def workloads_url(kind, ns, name=""):
    base = {"deployment": "/apis/apps/v1/namespaces/%s/deployments" % ns,
            "statefulset": "/apis/apps/v1/namespaces/%s/statefulsets" % ns}[kind]
    return base + ("/" + name if name else "")

def get_workload(kind, ns, name):
    return k8s("GET", workloads_url(kind, ns, name))

def restart(ns, kind, name):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    if kind in ("deployment", "statefulset"):
        k8s("PATCH", workloads_url(kind, ns, name),
            {"spec": {"template": {"metadata": {"annotations": {"selfheal.io/restartedAt": now}}}}},
            content_type="application/strategic-merge-patch+json")
        return "%s/%s 已触发滚动重启 (restartedAt=%s)" % (kind, name, now)
    if kind == "pod":
        k8s("DELETE", "/api/v1/namespaces/%s/pods/%s" % (ns, name))
        return "pod/%s 已删除, 等待副本控制器重建" % name
    raise ValueError("unsupported kind: %s" % kind)

def annotate(ns, kind, name, annotations):
    ann = {"selfheal.io/" + k: str(v)[:400] for k, v in annotations.items()}
    if kind in ("deployment", "statefulset"):
        k8s("PATCH", workloads_url(kind, ns, name),
            {"metadata": {"annotations": ann}},
            content_type="application/strategic-merge-patch+json")
    elif kind == "pod":
        k8s("PATCH", "/api/v1/namespaces/%s/pods/%s" % (ns, name),
            {"metadata": {"annotations": ann}},
            content_type="application/strategic-merge-patch+json")
    else:
        raise ValueError("unsupported kind: %s" % kind)
    return ann

def selector_pods(ns, selector):
    lbl = urllib.parse.urlencode(
        {"labelSelector": ",".join("%s=%s" % (k, v) for k, v in selector.items())})
    return k8s("GET", "/api/v1/namespaces/%s/pods?%s" % (ns, lbl)).get("items", [])

def _pod_snapshot(p):
    st = p.get("status", {})
    containers = []
    for c in st.get("containerStatuses") or []:
        s = c.get("state", {})
        waiting = (s.get("waiting") or {}).get("reason")
        last = (c.get("lastState", {}) or {}).get("terminated") or {}
        containers.append({
            "name": c.get("name"), "ready": c.get("ready"),
            "restartCount": c.get("restartCount", 0),
            "state": ("waiting:" + waiting) if waiting else (
                "terminated" if s.get("terminated") else "running"),
            "lastTerminated": {"reason": last.get("reason"),
                               "exitCode": last.get("exitCode"),
                               "finishedAt": last.get("finishedAt")} if last else None,
        })
    ready = bool(containers) and all(c["ready"] for c in containers)
    return {"pod": p.get("metadata", {}).get("name"), "phase": st.get("phase"),
            "ready": ready, "createdAt": p.get("metadata", {}).get("creationTimestamp"),
            "containers": containers}

def _pod_logs(ns, pod, tail=60):
    try:
        q = urllib.parse.urlencode({"tailLines": tail})
        return k8s("GET", "/api/v1/namespaces/%s/pods/%s/log?%s" % (ns, pod, q), raw=True)
    except Exception as e:
        return "(日志获取失败: %s)" % e

def _pod_events(ns, pod):
    try:
        fs = urllib.parse.urlencode({"fieldSelector": "involvedObject.name=%s" % pod})
        items = k8s("GET", "/api/v1/namespaces/%s/events?%s" % (ns, fs)).get("items", [])
        return [{"reason": e.get("reason"), "message": e.get("message"),
                 "lastAt": (e.get("lastTimestamp") or e.get("eventTime"))}
                for e in items[-10:]]
    except Exception:
        return []

def build_context(ns, kind, name):
    info = {"workload": None, "pods": [], "events": [], "logs": None}
    if kind in ("deployment", "statefulset"):
        w = get_workload(kind, ns, name)
        st = w.get("status", {})
        info["workload"] = {"kind": kind, "name": name,
                            "desiredReplicas": w.get("spec", {}).get("replicas"),
                            "availableReplicas": st.get("availableReplicas", 0),
                            "conditions": st.get("conditions", [])}
        pods = selector_pods(ns, w.get("spec", {}).get("selector", {}).get("matchLabels", {}))
    elif kind == "pod":
        pods = [k8s("GET", "/api/v1/namespaces/%s/pods/%s" % (ns, name))]
    else:
        return info
    info["pods"] = [_pod_snapshot(p) for p in pods]
    worst = max(info["pods"], key=lambda p: max([c["restartCount"] for c in p["containers"]] or [0]),
                default=None)
    if worst:
        info["events"] = _pod_events(ns, worst["pod"])
        info["logs"] = _pod_logs(ns, worst["pod"])
    return info

# ---------------------------------------------------------------- 健康检查

def _rfc3339(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()

def check_healthy(ns, kind, name):
    try:
        if kind in ("deployment", "statefulset"):
            w = get_workload(kind, ns, name)
            desired = w.get("spec", {}).get("replicas", 1)
            avail = w.get("status", {}).get("availableReplicas", 0)
            if avail < desired:
                return False, "可用副本 %d/%d" % (avail, desired)
            pods = selector_pods(ns, w.get("spec", {}).get("selector", {}).get("matchLabels", {}))
        elif kind == "pod":
            pods = [k8s("GET", "/api/v1/namespaces/%s/pods/%s" % (ns, name))]
        else:
            return True, "未知类型跳过健康检查"
        if not pods:
            return False, "未发现 Pod"
        for p in pods:
            pn = p.get("metadata", {}).get("name")
            st = p.get("status", {})
            if st.get("phase") != "Running":
                return False, "%s phase=%s" % (pn, st.get("phase"))
            for c in st.get("containerStatuses") or []:
                if not c.get("ready"):
                    return False, "%s/%s 未就绪" % (pn, c.get("name"))
                waiting = (c.get("state", {}).get("waiting") or {}).get("reason")
                if waiting in ("CrashLoopBackOff", "Error", "ImagePullBackOff"):
                    return False, "%s %s" % (pn, waiting)
            age = time.time() - _rfc3339(p.get("metadata", {}).get("creationTimestamp"))
            if age < 20:
                return False, "%s 重建后稳定中(%ds)" % (pn, int(age))
        return True, "所有副本 Ready"
    except Exception as e:
        return False, "健康检查异常: %s" % e

def alert_firing(alertname, ns):
    """查 Alertmanager: 告警是否仍 active。查询失败返回 None(未知)。"""
    try:
        with urllib.request.urlopen(AM_URL + "/api/v2/alerts", timeout=10) as r:
            for a in json.loads(r.read().decode()):
                lbl = a.get("labels", {})
                if lbl.get("alertname") == alertname and \
                   (not ns or lbl.get("namespace") == ns) and \
                   a.get("status", {}).get("state") == "active":
                    return True
        return False
    except Exception:
        return None

# ---------------------------------------------------------------- AI 诊断

RULE_HINTS = [
    ("OomKilled", "容器因内存超限被 OOMKilled。根因通常为内存泄漏、负载突增或 limits 设置过低。"
                  "处置: 已自动重启释放内存; 后续建议结合内存曲线确认是否需要上调 limits, 并排查泄漏点。"),
    ("CrashLoop", "容器处于反复重启(CrashLoopBackOff)。常见根因: 启动即崩溃、依赖服务不可达、配置错误。"
                  "处置: 已自动重启; 若重启后仍循环崩溃, 需结合日志 exitCode 与事件进一步人工排查。"),
    ("FailMode", "检测到应用主动注入的故障模式(演示): 实例内存持续泄漏, 非持久化故障。"
                 "自愈重启后新实例将以正常状态启动, 告警应随之解除; 若为 sticky 故障则重启无效, 需人工清理故障标记。"),
    ("NotReady", "节点处于 NotReady 状态。可能根因: kubelet 异常、容器运行时故障、网络分区或资源耗尽。"
                 "自愈链路不对节点执行重启, 建议检查 kubelet/运行时状态与节点资源水位。"),
]

def rules_diagnose(alertname, ctx):
    blob = json.dumps(ctx, ensure_ascii=False)
    for key, text in RULE_HINTS:
        if key.lower() in (alertname or "").lower() or key in blob:
            return text
    return ("告警触发, 已执行自动处置并持续观察。结合事件与日志未见明确模式时, "
            "建议人工核对最近的变更(发布/配置/依赖)与资源水位。")

def llm_diagnose(prompt):
    key = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if not key:
        return None
    base = (os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
    model = os.environ.get("OPENAI_MODEL") or "gpt-4o-mini"
    body = json.dumps({
        "model": model, "temperature": 0.2, "max_tokens": 700,
        "messages": [
            {"role": "system",
             "content": "你是资深 SRE。基于给定的告警与集群上下文, 用中文 Markdown 输出诊断: "
                        "## 根因分析\n## 影响评估\n## 处置建议\n## 预防措施。总长 300 字以内, 结论优先。"},
            {"role": "user", "content": prompt[:12000]},
        ]}).encode()
    req = urllib.request.Request(base + "/chat/completions", data=body, method="POST")
    req.add_header("Authorization", "Bearer " + key)
    req.add_header("Content-Type", "application/json")
    # 部分 OpenAI 兼容代理会拦截 Python-urllib 默认 UA (403), 使用常规 UA
    req.add_header("User-Agent", "heal-agent/1.2")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())["choices"][0]["message"]["content"]

# ---------------------------------------------------------------- 邮件通知

def send_email(subject, body, priority="normal"):
    host = (os.environ.get("SMTP_HOST") or "").strip()
    to = (os.environ.get("SMTP_TO") or "").strip()
    print("[notify] 邮件(%s) -> %s\n%s\n%s" % (priority, to or "(未配置收件人)", subject, body),
          flush=True)
    if not host or not to:
        return {"sent": False, "reason": "smtp_not_configured(内容已输出到 heal-agent 日志)"}
    user = (os.environ.get("SMTP_USER") or "").strip()
    passwd = (os.environ.get("SMTP_PASS") or "").strip()
    frm = (os.environ.get("SMTP_FROM") or user or "heal-agent@localhost")
    port = int(os.environ.get("SMTP_PORT") or "465")
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = frm
    msg["To"] = to
    if priority == "high":
        msg["X-Priority"] = "1"
        msg["X-MSMail-Priority"] = "High"
    starttls = (os.environ.get("SMTP_STARTTLS") or "false").lower() == "true"
    if starttls:
        smtp = smtplib.SMTP(host, port, timeout=30)
        smtp.ehlo()
        smtp.starttls(context=ssl.create_default_context())
    else:
        smtp = smtplib.SMTP_SSL(host, port, timeout=30)
    try:
        if user:
            smtp.login(user, passwd)
        smtp.sendmail(frm, to.split(","), msg.as_string())
    finally:
        smtp.quit()
    return {"sent": True}

# ---------------------------------------------------------------- 历史记录(闭环审计)

def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with _db_lock, sqlite3.connect(DB_PATH) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS heal_history(
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, alertname TEXT,
            namespace TEXT, target TEXT, result TEXT, engine TEXT,
            attempts INTEGER, duration_seconds REAL, diagnosis TEXT, note TEXT)""")

def record(d):
    dur = None
    starts = d.get("alert_starts_at")
    if starts:
        try:
            dur = round(time.time() - _rfc3339(starts), 1)
        except Exception:
            pass
    with _db_lock, sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO heal_history(ts,alertname,namespace,target,result,engine,attempts,"
            "duration_seconds,diagnosis,note) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(timespec="seconds"),
             d.get("alertname"), d.get("namespace"), d.get("target"), d.get("result"),
             d.get("engine"), d.get("attempts"), dur,
             (d.get("diagnosis") or "")[:3000], (d.get("note") or "")[:1000]))
        rid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    return {"id": rid, "duration_seconds": dur, "result": d.get("result"),
            "attempts": d.get("attempts")}

def history(limit=50):
    with _db_lock, sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM heal_history ORDER BY id DESC LIMIT ?", (int(limit),))]
        agg = {r[0]: (r[1] or 0) for r in conn.execute(
            "SELECT result, COUNT(*) FROM heal_history GROUP BY result")}
        mttr = conn.execute(
            "SELECT AVG(duration_seconds) FROM heal_history WHERE result='recovered'").fetchone()[0]
    return {"records": rows,
            "summary": {"total": sum(agg.values()), "recovered": agg.get("recovered", 0),
                        "escalated": agg.get("escalated", 0), "resolved": agg.get("resolved", 0),
                        "avg_recovery_seconds": round(mttr, 1) if mttr else None}}

# ---------------------------------------------------------------- HTTP 服务

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("[http] %s" % (fmt % args), flush=True)

    def _json(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n).decode() or "{}")

    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
        try:
            if u.path == "/healthz":
                return self._json(200, {"ok": True})
            if u.path == "/history":
                return self._json(200, history(q.get("limit", 50)))
            if u.path == "/verify":
                ns, kind, name = q.get("namespace", ""), q.get("kind", ""), q.get("name", "")
                healthy, detail = check_healthy(ns, kind, name)
                firing = alert_firing(q.get("alertname", ""), ns)
                recovered = healthy and (firing is not True)
                return self._json(200, {"healthy": healthy, "alert_firing": firing,
                                        "recovered": recovered, "detail": detail})
            if u.path == "/context":
                return self._json(200, build_context(q.get("namespace", ""),
                                                     q.get("kind", ""), q.get("name", "")))
            return self._json(404, {"error": "not found"})
        except Exception as e:
            return self._json(500, {"error": str(e)})

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        try:
            body = self._body()
            if u.path == "/heal/restart":
                msg = restart(body.get("namespace"), body.get("kind"), body.get("name"))
                print("[heal] %s" % msg, flush=True)
                return self._json(200, {"ok": True, "action": "restart", "detail": msg})
            if u.path == "/heal/annotate":
                anns = body.get("annotations")
                if not anns:
                    anns = {"last-result": body.get("result", "healed"),
                            "last-alert": body.get("alertname", ""),
                            "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
                ann = annotate(body.get("namespace"), body.get("kind"), body.get("name"), anns)
                return self._json(200, {"ok": True, "annotated": ann})
            if u.path == "/ai/diagnose":
                alert = body.get("alert") or {}
                ns, kind, name = alert.get("namespace", ""), alert.get("kind", ""), alert.get("name", "")
                ctx = build_context(ns, kind, name) if ns and name else {}
                prompt = "告警信息:\n%s\n\n集群上下文:\n%s" % (
                    json.dumps(alert, ensure_ascii=False, indent=1),
                    json.dumps(ctx, ensure_ascii=False, indent=1)[:9000])
                try:
                    diag = llm_diagnose(prompt)
                    engine = "llm" if diag else None
                except Exception as e:
                    print("[ai] LLM 调用失败: %s" % e, flush=True)
                    diag, engine = None, None
                if not diag:
                    diag, engine = rules_diagnose(alert.get("alertname"), ctx), "rules"
                return self._json(200, {"engine": engine, "diagnosis": diag})
            if u.path == "/notify/email":
                r = send_email(body.get("subject", "(无主题)"), body.get("body", ""),
                               body.get("priority", "normal"))
                return self._json(200, {"ok": True, **r})
            if u.path == "/record":
                return self._json(200, {"ok": True, **record(body)})
            return self._json(404, {"error": "not found"})
        except Exception as e:
            print("[error] %s %s -> %s" % (u.path, self.command, e), flush=True)
            return self._json(500, {"error": str(e)})

if __name__ == "__main__":
    init_db()
    print("[heal-agent] listening on :%d, AM=%s" % (PORT, AM_URL), flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
