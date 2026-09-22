#!/usr/bin/env python3
"""
demo-api: 自愈演示应用（纯标准库）
  POST /fail        注入故障: 内存泄漏 ~20MB/s -> 触发 OOM -> 告警 -> 自愈重启后恢复
  POST /fail?sticky=1 持久化故障标记(PVC): 重启也无法恢复 -> 触发"升级人工"路径
  POST /ok          清除故障(含 sticky 标记)
  GET  /healthz     健康检查 (始终 200)
  GET  /metrics     Prometheus 指标: demo_api_fail_mode 等
"""
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

PORT = 8080
FAIL_FILE = "/data/sticky-fail"
state = {"fail": False, "sticky": False, "leaked": 0, "oom_count": 0}
leak = bytearray()
_boot = time.time()

# sticky 标记随 PVC 持久化: 重启后自动恢复故障状态
if os.path.exists(FAIL_FILE):
    state["fail"] = True
    state["sticky"] = True
    print("[demo] 检测到 sticky 故障标记, 启动即进入故障模式", flush=True)


def leak_loop():
    while True:
        if state["fail"]:
            try:
                leak.extend(b"x" * (8 * 1024 * 1024))
                state["leaked"] = len(leak)
            except MemoryError:
                pass
            time.sleep(2.5)  # ~3MB/s, 留出告警触发窗口(约30s后OOM)
        else:
            time.sleep(0.5)


threading.Thread(target=leak_loop, daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("[http] %s" % (fmt % args), flush=True)

    def _text(self, code, text):
        data = text.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n:
            self.rfile.read(n)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/healthz":
            return self._text(200, "ok\n")
        if u.path == "/metrics":
            return self._text(200, (
                "# HELP demo_api_fail_mode 是否处于故障注入模式\n"
                "# TYPE demo_api_fail_mode gauge\n"
                "demo_api_fail_mode %d\n"
                "# HELP demo_api_sticky_fail_mode 故障标记是否持久化(重启不可恢复)\n"
                "# TYPE demo_api_sticky_fail_mode gauge\n"
                "demo_api_sticky_fail_mode %d\n"
                "# HELP demo_api_leaked_bytes 已泄漏字节数\n"
                "# TYPE demo_api_leaked_bytes gauge\n"
                "demo_api_leaked_bytes %d\n"
                "# HELP demo_api_uptime_seconds 进程运行秒数\n"
                "# TYPE demo_api_uptime_seconds gauge\n"
                "demo_api_uptime_seconds %.0f\n"
            ) % (1 if state["fail"] else 0, 1 if state["sticky"] else 0,
                 state["leaked"], time.time() - _boot))
        if u.path == "/":
            return self._text(200, json.dumps(
                {"status": "FAIL_MODE" if state["fail"] else "healthy",
                 "sticky": state["sticky"], "leaked_mb": round(state["leaked"] / 1048576, 1),
                 "usage": {"inject": "POST /fail  (加 ?sticky=1 模拟重启不可恢复)",
                           "recover": "POST /ok"}}, ensure_ascii=False))
        return self._text(404, "not found\n")

    def do_POST(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        self._read_body()
        if u.path == "/fail":
            state["fail"] = True
            if q.get("sticky", ["0"])[0] in ("1", "true"):
                os.makedirs(os.path.dirname(FAIL_FILE), exist_ok=True)
                with open(FAIL_FILE, "w") as f:
                    f.write(time.strftime("%Y-%m-%dT%H:%M:%S"))
                state["sticky"] = True
            mode = "sticky(重启不可恢复)" if state["sticky"] else "volatile(重启可恢复)"
            return self._text(200, "故障已注入: 内存泄漏, 模式=%s\n" % mode)
        if u.path == "/ok":
            state["fail"] = False
            state["sticky"] = False
            state["leaked"] = 0
            leak.clear()
            if os.path.exists(FAIL_FILE):
                os.remove(FAIL_FILE)
            return self._text(200, "故障已清除, 应用恢复健康\n")
        return self._text(404, "not found\n")


if __name__ == "__main__":
    print("[demo-api] listening on :%d" % PORT, flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
