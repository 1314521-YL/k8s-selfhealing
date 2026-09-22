#!/bin/bash
# 02: 部署 n8n 并通过 REST API 导入/更新激活"告警闭环处置"工作流 (幂等)
set -e
cd "$(dirname "$0")/.."
kubectl apply -f n8n/n8n.yaml
kubectl -n monitoring rollout status deploy/n8n --timeout=300s
newpod() { kubectl -n monitoring get pod -l app=n8n --field-selector=status.phase=Running -o jsonpath='{.items[0].metadata.name}'; }

POD=$(newpod)
kubectl cp n8n/workflow-alert-handler.json "monitoring/$POD:/tmp/wf.json"

# 通过 Public API upsert 工作流 (内部 REST 不支持更新)
kubectl -n monitoring exec "$POD" -- node -e "
const fs = require('fs');
const wf = JSON.parse(fs.readFileSync('/tmp/wf.json', 'utf8'));
(async () => {
  const lr = await fetch('http://localhost:5678/rest/login', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({email: 'admin@selfheal.local', password: 'Selfheal#2026'})});
  const cookie = (lr.headers.getSetCookie ? lr.headers.getSetCookie() : [lr.headers.get('set-cookie')])
    .map(c => c.split(';')[0]).join('; ');
  let keys = (await (await fetch('http://localhost:5678/rest/api-keys', {headers: {cookie}})).json()).data || [];
  // 列表返回掩码 key(含*), 不可用; 删除旧 key 后重建拿原文
  let key = (keys.find(k => k.apiKey && k.apiKey.indexOf('*') === -1) || {}).apiKey || null;
  if (!key) {
    for (const k of keys) {
      await fetch('http://localhost:5678/rest/api-keys/' + k.id, {method: 'DELETE', headers: {cookie, 'browser-id': 'deploy'}});
    }
    const kr = await fetch('http://localhost:5678/rest/api-keys', {method: 'POST',
      headers: {cookie, 'Content-Type': 'application/json', 'browser-id': 'deploy'},
      body: JSON.stringify({label: 'deploy-script'})});
    key = (await kr.json()).data.apiKey;
  }
  const H = {'X-N8N-API-KEY': key, 'Content-Type': 'application/json'};
  const body = JSON.stringify({name: wf.name, nodes: wf.nodes, connections: wf.connections,
    settings: wf.settings, staticData: null});
  const list = await (await fetch('http://localhost:5678/api/v1/workflows?limit=250', {headers: H})).json();
  const existing = (list.data || []).find(w => w.name === wf.name);
  let r, id;
  if (existing) {
    id = existing.id;
    r = await fetch('http://localhost:5678/api/v1/workflows/' + id, {method: 'PUT', headers: H, body});
    console.log('updated workflow', id, r.status);
  } else {
    r = await fetch('http://localhost:5678/api/v1/workflows', {method: 'POST', headers: H, body});
    const d = await r.json();
    id = d.data && d.data.id;
    console.log('created workflow', id, r.status);
  }
  if (!r.ok) console.log((await r.text()).slice(0, 300));
  console.log('WF_ID=' + id);
})().catch(e => { console.log('ERR', String(e).slice(0, 300)); process.exit(1); });"

WF_ID=$(kubectl -n monitoring exec "$POD" -- node -e "
(async () => {
  const lr = await fetch('http://localhost:5678/rest/login', {method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({email: 'admin@selfheal.local', password: 'Selfheal#2026'})});
  const cookie = (lr.headers.getSetCookie ? lr.headers.getSetCookie() : [lr.headers.get('set-cookie')])
    .map(c => c.split(';')[0]).join('; ');
  const keys = (await (await fetch('http://localhost:5678/rest/api-keys', {headers: {cookie}})).json()).data || [];
  let key = (keys.find(k => k.apiKey && k.apiKey.indexOf('*') === -1) || {}).apiKey;
  if (!key) {
    for (const k of keys) {
      await fetch('http://localhost:5678/rest/api-keys/' + k.id, {method: 'DELETE', headers: {cookie, 'browser-id': 'deploy'}});
    }
    const kr = await fetch('http://localhost:5678/rest/api-keys', {method: 'POST',
      headers: {cookie, 'Content-Type': 'application/json', 'browser-id': 'deploy'},
      body: JSON.stringify({label: 'deploy-script'})});
    key = (await kr.json()).data.apiKey;
  }
  if (!key) { console.log(''); return; }
  const H = {'X-N8N-API-KEY': key};
  const list = await (await fetch('http://localhost:5678/api/v1/workflows?limit=250', {headers: H})).json();
  const w = (list.data || []).find(w => w.name === 'Alertmanager 自愈闭环处置');
  console.log(w ? w.id : '');
})().catch(() => process.exit(1));")
[ -n "$WF_ID" ] || { echo "✗ 未找到工作流"; exit 1; }
echo "WF_ID=$WF_ID"

# 激活并重启以注册生产 Webhook
POD=$(newpod)
kubectl -n monitoring exec "$POD" -- n8n update:workflow --id="$WF_ID" --active=true
kubectl -n monitoring rollout restart deploy/n8n
kubectl -n monitoring rollout status deploy/n8n --timeout=300s
sleep 3

echo "--> 工作流状态:"
kubectl -n monitoring exec "$(newpod)" -- n8n list:workflow 2>/dev/null | grep -v "User settings" || true
echo "✓ n8n 就绪: http://<NODE_IP>:30567  (admin@selfheal.local / Selfheal#2026, 请尽快修改)"
