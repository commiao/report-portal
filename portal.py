#!/usr/bin/env python3
"""report-portal — a standalone, source-agnostic report/dashboard aggregator.

WHY THIS IS ITS OWN SERVICE (not part of kg-hub)
------------------------------------------------
kg-hub (knowledge capsules) is just *one* data source. As more panels / reports /
features come online they should not all be coupled into kg_hub_server.py. So the
portal is a thin shell: it owns navigation + shared chrome, and aggregates cards
from each source. Each source keeps owning and rendering its own dashboards
(next to its own data) and exposes a tiny `/portal_manifest` JSON listing its
cards. Adding a source = one entry in SOURCES (env), no code change here.

HOW IT TALKS TO SOURCES
-----------------------
Each source has two base URLs because the portal and the user's browser sit in
different network positions:
  - fetch_base : reachable from THIS container (server-side manifest fetch).
                 On the NAS we share kg-hub's docker network, so this is the
                 compose service name, e.g. http://kg_hub_server:8080
  - link_base  : reachable from the USER'S browser (the tailnet address), so the
                 rendered card links are clickable, e.g. http://100.123.208.32:17171
Card `url`s in a manifest are relative; the portal rewrites them to link_base+url.

Sources are configured via the PORTAL_SOURCES env var (JSON). A sane default
wires up kg-hub so the service runs out of the box.
"""
import asyncio
import json
import os

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

# ---------- Source registry (override with PORTAL_SOURCES env, JSON) ----------
# Each: {id, name, fetch_base, link_base, manifest}
_DEFAULT_SOURCES = [
    {
        "id": "kg-hub",
        "name": "kg-hub 知识胶囊",
        "fetch_base": os.environ.get("KGHUB_FETCH_BASE", "http://kg_hub_server:8080"),
        "link_base": os.environ.get("KGHUB_LINK_BASE", "http://100.123.208.32:17171"),
        "manifest": "/portal_manifest",
    },
]


def _load_sources():
    raw = os.environ.get("PORTAL_SOURCES")
    if not raw:
        return _DEFAULT_SOURCES
    try:
        srcs = json.loads(raw)
        assert isinstance(srcs, list)
        return srcs
    except Exception:  # noqa: BLE001 — bad config must not crash the portal
        return _DEFAULT_SOURCES


SOURCES = _load_sources()
FETCH_TIMEOUT = float(os.environ.get("PORTAL_FETCH_TIMEOUT", "5"))


async def _fetch_source(client: httpx.AsyncClient, src: dict) -> dict:
    """Fetch one source's manifest. Never raises — degrades to an error marker
    so one unreachable source can't take down the whole portal."""
    sid = src.get("id", "?")
    name = src.get("name", sid)
    link_base = (src.get("link_base") or "").rstrip("/")
    url = (src.get("fetch_base") or "").rstrip("/") + src.get("manifest", "/portal_manifest")
    try:
        resp = await client.get(url)
        resp.raise_for_status()
        payload = resp.json()
        reports = payload.get("reports", payload if isinstance(payload, list) else [])
        cards = []
        for r in reports:
            cards.append({
                "name": r.get("name", "?"),
                "desc": r.get("desc", ""),
                "icon": r.get("icon", "📄"),
                "ready": bool(r.get("ready", True)),
                "url": link_base + r.get("url", "") if r.get("url", "").startswith("/") else r.get("url", ""),
            })
        return {"id": sid, "name": name, "ok": True, "cards": cards}
    except Exception as exc:  # noqa: BLE001
        return {"id": sid, "name": name, "ok": False, "error": f"{type(exc).__name__}", "cards": []}


async def _gather():
    async with httpx.AsyncClient(timeout=FETCH_TIMEOUT) as client:
        return await asyncio.gather(*[_fetch_source(client, s) for s in SOURCES])


_PORTAL_HTML = """<!doctype html><html lang=zh><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><meta http-equiv=refresh content=120>
<title>报表门户</title>
<style>:root{color-scheme:light dark}
body{font-family:-apple-system,system-ui,"PingFang SC",sans-serif;max-width:900px;margin:2rem auto;padding:0 1rem;background:Canvas;color:CanvasText;line-height:1.6}
h1{font-size:20px;font-weight:500;margin:.2rem 0}.sub{color:GrayText;font-size:13px;margin-bottom:1.5rem}
.src{font-size:13px;color:GrayText;margin:1.6rem 0 .5rem;display:flex;align-items:center;gap:8px}
.src .dot{width:7px;height:7px;border-radius:50%}.ok{background:#2EA043}.bad{background:#D1242F}
.src .err{color:#D1242F}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px}
a.card{display:block;text-decoration:none;color:inherit;border:1px solid color-mix(in srgb,CanvasText 18%,transparent);border-radius:12px;padding:1rem 1.1rem}
a.card:hover{border-color:color-mix(in srgb,CanvasText 45%,transparent)}
.t{font-size:15px;font-weight:500}.d{font-size:13px;color:GrayText;margin-top:4px}
.soon{opacity:.5;pointer-events:none}.empty{color:GrayText;font-size:13px}
.foot{color:GrayText;font-size:12px;margin-top:2.5rem}</style></head><body>
<h1>报表门户</h1><div class=sub>多源报表 / 看板的统一入口 · 独立服务·按源聚合 · 每 120s 刷新</div>
<div id=body></div>
<div class=foot>新增数据源：在 report-portal 的 PORTAL_SOURCES 加一条；新增报表：在对应源的 /portal_manifest 里加一张卡。</div>
<script>var D=__DATA__;
document.getElementById('body').innerHTML=D.map(function(s){
var head='<div class=src><span class="dot '+(s.ok?'ok':'bad')+'"></span>'+s.name+(s.ok?'':' <span class=err>· 暂不可达 ('+s.error+')</span>')+'</div>';
var body;
if(!s.cards.length){body='<div class=empty>'+(s.ok?'该源暂无报表':'无法获取卡片')+'</div>';}
else{body='<div class=grid>'+s.cards.map(function(r){
return '<a class="card'+(r.ready?'':' soon')+'" href="'+r.url+'"><div class=t>'+r.icon+' '+r.name+(r.ready?'':' · 即将上线')+'</div><div class=d>'+r.desc+'</div></a>';}).join('')+'</div>';}
return head+body;}).join('');</script></body></html>"""


async def portal(request: Request) -> HTMLResponse:
    data = await _gather()
    return HTMLResponse(_PORTAL_HTML.replace("__DATA__", json.dumps(data, ensure_ascii=False)))


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "sources": [s.get("id") for s in SOURCES]})


app = Starlette(
    debug=False,
    routes=[
        Route("/", portal, methods=["GET"]),
        Route("/portal", portal, methods=["GET"]),
        Route("/health", health, methods=["GET"]),
    ],
)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host=os.environ.get("PORTAL_BIND_HOST", "0.0.0.0"),
        port=int(os.environ.get("PORTAL_BIND_PORT", "8080")),
    )
