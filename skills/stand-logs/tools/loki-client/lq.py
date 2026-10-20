#!/usr/bin/env python3
# Запасной клиент к Loki без сборки (только стандартная библиотека Python 3), сырой LogQL.
# Конфиг — из тех же env-переменных, что и lokilogs (main.go): LOKI_URL, LOKI_CERT, LOKI_KEY, LOKI_ORG.
# Если LOKI_CERT/LOKI_KEY не заданы — ~/.config/loki-client/{client.pem,client.key}
# (сертификатов в репозитории нет и быть не должно, их выдают лично).
import argparse, json, ssl, urllib.request, urllib.parse, datetime, os, sys
BASE=os.environ.get("LOKI_URL","").rstrip("/")
if not BASE: sys.exit("нужен env LOKI_URL (обычно: . ~/.config/loki-client/env)")
CERT=os.environ.get("LOKI_CERT", os.path.expanduser("~/.config/loki-client/client.pem"))
KEY=os.environ.get("LOKI_KEY", os.path.expanduser("~/.config/loki-client/client.key"))
ORG=os.environ.get("LOKI_ORG","")
def ctx():
    c=ssl.create_default_context(); c.check_hostname=False; c.verify_mode=ssl.CERT_NONE
    c.load_cert_chain(CERT, KEY); return c
def tons(s):
    dt=datetime.datetime.strptime(s,"%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    return int(dt.timestamp()*1e9)
p=argparse.ArgumentParser()
p.add_argument("--query",required=True); p.add_argument("--start"); p.add_argument("--end")
p.add_argument("--limit",type=int,default=2000); p.add_argument("--direction",default="forward")
p.add_argument("--instant",action="store_true"); p.add_argument("--step"); p.add_argument("--raw",action="store_true")
a=p.parse_args()
if a.instant:
    vals={"query":a.query,"time":str(tons(a.end))}; path="/loki/api/v1/query"
else:
    vals={"query":a.query,"start":str(tons(a.start)),"end":str(tons(a.end)),"limit":str(min(a.limit,5000)),"direction":a.direction}
    if a.step: vals["step"]=a.step
    path="/loki/api/v1/query_range"
req=urllib.request.Request(BASE+path+"?"+urllib.parse.urlencode(vals))
if ORG and not ORG.startswith("REPLACE"): req.add_header("X-Scope-OrgID", ORG)
d=json.load(urllib.request.urlopen(req,timeout=60,context=ctx()))
if d.get("status")!="success": print(json.dumps(d)[:2000]); sys.exit(1)
res=d["data"]["result"]
if a.instant or d["data"].get("resultType")!="streams":
    print(json.dumps(res)); sys.exit(0)
rows=[]
for st in res:
    for v in st["values"]:
        t=int(v[0]); line=v[1].rstrip("\n")
        pod=st["stream"].get("pod") or st["stream"].get("hostname","")
        msg=line
        if not a.raw:
            try:
                e=json.loads(line); msg=e.get("message",line)
                pod=(e.get("kubernetes") or {}).get("pod_name") or pod
            except Exception: pass
        rows.append((t,pod,msg))
rows.sort()
for t,pod,msg in rows:
    ts=datetime.datetime.fromtimestamp(t/1e9,datetime.timezone.utc).strftime("%H:%M:%S.%f")[:-3]
    print(f"{ts}Z [{pod}] {msg}")
print(f"# lines={len(rows)}", file=sys.stderr)
