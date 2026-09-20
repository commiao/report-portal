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
import time

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

# ---------- Source registry (override with PORTAL_SOURCES env, JSON) ----------
# A source is EITHER:
#   - manifest-based: {id, name, fetch_base, link_base, manifest}
#       portal fetches fetch_base+manifest server-side and merges the cards.
#   - static:         {id, name, cards: [{name,desc,url,icon,ready}], link_base?}
#       no fetch — cards are declared inline. Use this for sources that don't (yet)
#       expose /portal_manifest, or single-page dashboards on another tailnet host:
#       the card link is opened by the user's BROWSER (tailnet-reachable), so the
#       portal container never needs to reach that host. Absolute card urls are
#       used as-is; relative urls get link_base prefixed.
_DEFAULT_SOURCES = [
    {
        "id": "kg-hub",
        "name": "kg-hub 知识胶囊",
        "fetch_base": os.environ.get("KGHUB_FETCH_BASE", "http://kg_hub_server:8080"),
        "link_base": os.environ.get("KGHUB_LINK_BASE", "http://100.123.208.32:17171"),
        "manifest": "/portal_manifest",
    },
    {
        "id": "openclaw-finance",
        "name": "OpenClaw 财务",
        "cards": [
            {
                "name": "财务看板",
                "desc": "OpenClaw 财务看板（收支 / 成本概览）",
                "url": os.environ.get("FINANCE_URL", "http://100.79.177.102:18765/finance"),
                "icon": "💰",
                "ready": True,
            },
        ],
    },
    {
        # NOTE: 由另一个协同会话加入（曾只在 NAS 运行容器里、未进 git）；
        # 2026-08-18 抢救合并进 git，避免多 actor 共享目录导致丢失。
        "id": "openclaw-content-ops",
        "name": "OpenClaw 内容运营",
        "cards": [
            {
                "name": "内容运营看板",
                "desc": "公众号草稿、掘金实验、指标待办和内容契约健康",
                "url": os.environ.get("CONTENT_OPS_URL", "http://100.79.177.102:18766/content-ops"),
                "icon": "✍️",
                "ready": True,
            },
        ],
    },
    {
        "id": "skill-sync",
        "name": "跨设备工具同步",
        "cards": [
            {
                "name": "工具同步面板",
                "desc": "跨设备 skill / 工具同步管理（Skill Sync Sidecar）",
                "url": os.environ.get("SKILL_SYNC_URL", "http://100.123.208.32:8765"),
                "icon": "🔄",
                "ready": True,
            },
        ],
    },
    {
        "id": "openclaw-recruit",
        "name": "OpenClaw 招聘情报",
        "cards": [
            {
                "name": "招聘情报面板",
                "desc": "OpenClaw 招聘情报看板",
                "url": os.environ.get("RECRUIT_URL", "http://100.123.208.32:18180"),
                "icon": "🧑‍💼",
                "ready": True,
            },
        ],
    },
    {
        "id": "task-hub",
        "name": "task-hub 任务",
        "cards": [
            {
                "name": "任务看板",
                "desc": "task-hub 跨工具任务系统看板",
                "url": os.environ.get("TASK_HUB_URL", "http://100.123.208.32:17173/ui"),
                "icon": "📋",
                "ready": True,
            },
        ],
    },
    {
        "id": "model-gateway",
        "name": "模型与凭证",
        "cards": [
            {
                "name": "NAS 模型网关",
                "desc": "按业务 Key 选择模型，并新增或更换加密 API Key 凭证",
                "url": os.environ.get(
                    "MODEL_GATEWAY_DASHBOARD_URL",
                    "http://100.123.208.32:39010",
                ),
                "icon": "🔐",
                "ready": True,
            },
        ],
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


def _norm_cards(reports, link_base: str) -> list:
    """Normalize a list of card dicts: fill defaults, prefix relative urls with
    link_base. Shared by the manifest and static paths."""
    cards = []
    for r in reports:
        u = r.get("url", "")
        cards.append({
            "name": r.get("name", "?"),
            "desc": r.get("desc", ""),
            "icon": r.get("icon", "📄"),
            "ready": bool(r.get("ready", True)),
            "url": link_base + u if u.startswith("/") else u,
        })
    return cards


async def _fetch_source(client: httpx.AsyncClient, src: dict) -> dict:
    """Resolve one source's cards. Never raises — degrades to an error marker so
    one unreachable source can't take down the whole portal. A source with inline
    `cards` is static (no fetch); otherwise its manifest is fetched."""
    sid = src.get("id", "?")
    name = src.get("name", sid)
    link_base = (src.get("link_base") or "").rstrip("/")

    if src.get("cards") is not None:  # static source — no network call
        return {"id": sid, "name": name, "ok": True,
                "cards": _norm_cards(src["cards"], link_base)}

    url = (src.get("fetch_base") or "").rstrip("/") + src.get("manifest", "/portal_manifest")
    try:
        resp = await client.get(url)
        resp.raise_for_status()
        payload = resp.json()
        reports = payload.get("reports", payload if isinstance(payload, list) else [])
        return {"id": sid, "name": name, "ok": True, "cards": _norm_cards(reports, link_base)}
    except Exception as exc:  # noqa: BLE001
        return {"id": sid, "name": name, "ok": False, "error": f"{type(exc).__name__}", "cards": []}


async def _gather():
    async with httpx.AsyncClient(timeout=FETCH_TIMEOUT) as client:
        return await asyncio.gather(*[_fetch_source(client, s) for s in SOURCES])


# Two view modes over the same data:
#   v-list — grouped by source (header + rows), descriptions visible.
#   v-tile — ALL cards flattened into one flowing grid, source demoted to a
#            caption on each tile. Flattening matters: every source currently
#            holds a single card, so keeping the grouping in tile mode renders
#            one lonely tile per row and wastes the whole screen.
# Switching re-renders the body and swaps the <body> class. The choice is kept
# in localStorage — required, because the page self-refreshes every 120s.
_PORTAL_HTML = """<!doctype html><html lang=zh><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1"><meta http-equiv=refresh content=120>
<title>报表门户</title>
<style>:root{color-scheme:light dark}
body{font-family:-apple-system,system-ui,"PingFang SC",sans-serif;max-width:900px;margin:2rem auto;padding:0 1rem;background:Canvas;color:CanvasText;line-height:1.6}
h1{font-size:20px;font-weight:500;margin:.2rem 0}.sub{color:GrayText;font-size:13px}
.bar{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;flex-wrap:wrap;margin-bottom:1.2rem}
.views{display:flex;gap:6px;flex:none;padding-top:.3rem}
.views button{font:inherit;font-size:12.5px;padding:4px 10px;border-radius:8px;cursor:pointer;
border:1px solid color-mix(in srgb,CanvasText 20%,transparent);background:transparent;color:GrayText}
.views button[aria-pressed=true]{background:color-mix(in srgb,CanvasText 10%,transparent);color:CanvasText}
.src{font-size:13px;color:GrayText;margin:1.6rem 0 .5rem;display:flex;align-items:center;gap:8px}
.src .dot{width:7px;height:7px;border-radius:50%}.ok{background:#2EA043}.bad{background:#D1242F}
.src .err{color:#D1242F}
.card{text-decoration:none;color:inherit;border:1px solid color-mix(in srgb,CanvasText 18%,transparent);border-radius:12px}
a.card:hover{border-color:color-mix(in srgb,CanvasText 45%,transparent)}
.soon{opacity:.5;pointer-events:none}.empty{color:GrayText;font-size:13px}
.foot{color:GrayText;font-size:12px;margin-top:2.5rem}
.v-list .grid{display:flex;flex-direction:column;gap:8px}
.v-list .card{display:flex;align-items:center;gap:10px;padding:.55rem .9rem}
.v-list .ic{font-size:16px;flex:none}
.v-list .t{font-size:14px;font-weight:500;flex:none}
.v-list .d{font-size:12.5px;color:GrayText;margin-left:auto;padding-left:14px;text-align:right;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.v-list .s{display:none}
.v-tile .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(132px,1fr));gap:12px}
.v-tile .card{display:block;text-align:center;padding:1rem .6rem}
.v-tile .ic{font-size:30px;line-height:1.25;display:block;margin-bottom:.4rem}
.v-tile .t{font-size:13px;font-weight:500;display:block;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.v-tile .d{display:none}
.v-tile .s{display:block;font-size:11px;color:GrayText;margin-top:3px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.v-tile .dead{opacity:.55;cursor:default}
@media(max-width:520px){.v-list .d{display:none}}</style></head><body class=v-list>
<div class=bar>
<div><h1>报表门户</h1><div class=sub>多源报表 / 看板的统一入口 · 按源聚合 · 每 120s 刷新</div></div>
<div class=views><button data-v=list aria-pressed=true>☰ 列表</button><button data-v=tile aria-pressed=false>▦ 缩略图</button></div>
</div>
<div id=body></div>
<div class=foot>新增数据源：在 report-portal 的 PORTAL_SOURCES 加一条；新增报表：在对应源的 /portal_manifest 里加一张卡。</div>
<script>var D=__DATA__;
function esc(s){return String(s==null?'':s).replace(/[&<>"]/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c];});}
function card(r,srcName){
return '<a class="card'+(r.ready?'':' soon')+'" href="'+esc(r.url)+'" title="'+esc(r.name)+(r.desc?' — '+esc(r.desc):'')+'">'
+'<span class=ic>'+esc(r.icon)+'</span><span class=t>'+esc(r.name)+(r.ready?'':' · 即将上线')+'</span>'
+'<span class=d>'+esc(r.desc)+'</span><span class=s>'+esc(srcName)+'</span></a>';}
function renderList(){return D.map(function(s){
var head='<div class=src><span class="dot '+(s.ok?'ok':'bad')+'"></span>'+esc(s.name)+(s.ok?'':' <span class=err>· 暂不可达 ('+esc(s.error)+')</span>')+'</div>';
var body=s.cards.length?'<div class=grid>'+s.cards.map(function(r){return card(r,s.name);}).join('')+'</div>'
:'<div class=empty>'+(s.ok?'该源暂无报表':'无法获取卡片')+'</div>';
return head+body;}).join('');}
function renderTile(){var out=[];
D.forEach(function(s){
if(s.cards.length){s.cards.forEach(function(r){out.push(card(r,s.name));});}
else{out.push('<span class="card dead" title="'+esc(s.name)+(s.ok?' — 暂无报表':' — 暂不可达 ('+esc(s.error)+')')+'">'
+'<span class=ic>'+(s.ok?'📭':'⚠️')+'</span><span class=t>'+esc(s.name)+'</span>'
+'<span class=s>'+(s.ok?'暂无报表':'暂不可达')+'</span></span>');}});
return '<div class=grid>'+out.join('')+'</div>';}
var KEY='portal.view',BTN=document.querySelectorAll('.views button'),BODY=document.getElementById('body');
function setView(v){document.body.className='v-'+v;
BODY.innerHTML=(v==='tile'?renderTile():renderList());
Array.prototype.forEach.call(BTN,function(b){b.setAttribute('aria-pressed',String(b.dataset.v===v));});
try{localStorage.setItem(KEY,v);}catch(e){}}
var saved='list';try{saved=localStorage.getItem(KEY)||'list';}catch(e){}
setView(saved==='tile'?'tile':'list');
Array.prototype.forEach.call(BTN,function(b){b.onclick=function(){setView(b.dataset.v);};});</script></body></html>"""


PLACEHOLDER = "__DATA__"


def render_portal(data: list) -> str:
    """把聚合结果渲染成页面。

    portal() 和 health() 都走这一个函数，而不是各自 `.replace` 一遍：判断的两端
    取自不同来源时，迟早会报一件不存在的事（准则 28）。health 要是渲染的是另一
    条路径，它验证的就不是用户实际拿到的东西。

    替换没生效就**抛**，不返回半成品：模板里一旦把 `__DATA__` 改名，`.replace`
    是一次静默无操作——页面照常 200、结构完整、一张卡都没有。字符串替换不会报
    错，所以必须在这里自己查。查的是「数据真的嵌进去了」而不是「占位符还在不在」：
    占位符被改名之后，html 里本来就找不到旧名字，按后者查恰好漏掉要抓的那种。
    """
    payload = json.dumps(data, ensure_ascii=False)
    html = _PORTAL_HTML.replace(PLACEHOLDER, payload)
    if payload not in html:
        raise RuntimeError(
            f"模板里没有 {PLACEHOLDER} 占位符，数据没被嵌进页面（页面会渲染成空的）")
    return html


async def portal(request: Request) -> HTMLResponse:
    return HTMLResponse(render_portal(await _gather()))


async def health(request: Request) -> JSONResponse:
    """量的是「这个门户现在还能不能把页面做出来」，不是「进程还活着吗」。

    原来这里回显 `SOURCES` 里的 id —— 聚合坏了、渲染坏了、模板占位符被改名，它
    照样 200 ok。那正是准则 9 的三个出处：kg-hub 日报读一个已退役组件的水位线，
    96 天每天都报「系统正常在线」；网关 /health/ready 返回 ok 而每一次真实调用
    都 503；refinery status.json 的时间戳每轮都刷新，而里面大半字段是上一个容器
    留下的。

    三档的分界线是**这件事该由谁负责**：
      down      聚合抛了、或渲染出来的东西不能用 —— 门户自己坏了（503）
      degraded  某些源不可达 —— 那是门户如实上报的数据，不是它的故障（200）
      ok        全部源都拿到了
    degraded 刻意不 503：门户的活是聚合与导航，别人家面板停机不该让 release.sh
    把门户自动回滚掉。
    """
    started = time.monotonic()
    try:
        data = await _gather()
        html = render_portal(data)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(
            {"status": "down", "reason": "聚合或渲染抛异常",
             "error": f"{type(exc).__name__}: {exc}"},
            status_code=503,
        )

    failed = [s["id"] for s in data if not s.get("ok")]
    cards = sum(len(s.get("cards") or []) for s in data)

    # 渲染是否真的把数据嵌进去了，由 render_portal 自己把关（替换没生效会抛，
    # 上面的 except 接住）。这里只剩「聚合出来是空的」：页面还是 200、结构完整、
    # 一张卡都没有——活着但不干活。
    if cards == 0:
        return JSONResponse(
            {"status": "down", "reason": "一张卡片都聚合不到",
             "sources": {"total": len(data), "failed": failed}, "cards": 0},
            status_code=503,
        )

    return JSONResponse({
        "status": "degraded" if failed else "ok",
        "sources": {"total": len(data), "ok": len(data) - len(failed), "failed": failed},
        "cards": cards,
        "rendered_bytes": len(html),
        "took_ms": round((time.monotonic() - started) * 1000),
    })


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
