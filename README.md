# report-portal — 多源报表门户（独立服务）

报表/看板的统一入口。**独立于任何数据源**：kg-hub 的知识胶囊只是其中一个源。

- 入口：`http://100.123.208.32:17172/portal`（tailnet 内任意设备，无需 token）
- 运行：NAS 上独立 Docker 容器 `report-portal`（compose 项目名 `report-portal`，端口 `127.0.0.1:17172` → 经 tailscale 暴露）
- 它**不读任何数据库**：只按 `SOURCES` 抓各源的 `/portal_manifest`，合并渲染卡片网格。

## 架构：瘦门户 + 各源自持看板

```
        report-portal (本服务, :17172)         各数据源(各自的容器/进程)
        ├─ 抓 /portal_manifest  ───────────▶  kg-hub-server (:17171)
        │   合并所有源的卡片                    ├─ /portal_manifest  (列出自己的卡片)
        │   渲染统一门户首页                    ├─ /dashboard/capsules (自持看板)
        └─ 卡片链接跳到各源看板                 └─ /dashboard/usage    (自持看板)
```

- **门户只管聚合与导航**，不替任何后端读库——看板留在数据旁边（如 kg-hub 看板直读 NAS-local 的 FalkorDB）。
- **加一个数据源** = 在 `PORTAL_SOURCES` 加一条，门户代码不动。
- **加一个报表** = 在那个源的 `/portal_manifest` 里加一张卡（+ 该源自己写 `/dashboard/*` 处理器）。

## 源配置

每个源两个 base（门户和浏览器处于不同网络位置）：
- `fetch_base`：**本容器**可达，用于服务端抓 manifest。NAS 上共享 kg-hub 的 docker 网络，所以用 compose 服务名 `http://kg_hub_server:8080`。
- `link_base`：**用户浏览器**可达（tailnet 地址），用于渲染可点击的卡片链接，如 `http://100.123.208.32:17171`。

manifest 里卡片的 `url` 用相对路径，门户渲染时拼成 `link_base + url`。

默认源写死在 `portal.py`，可用环境变量 `PORTAL_SOURCES`（JSON 数组）整体覆盖；单独覆盖 kg-hub 的两个 base 用 `KGHUB_FETCH_BASE` / `KGHUB_LINK_BASE`。

源有两种形态：

- **manifest 源**（如 kg-hub）：`{id, name, fetch_base, link_base, manifest}` —— 门户服务端抓 `fetch_base+manifest` 合并卡片。适合自己会暴露 `/portal_manifest` 的服务。
- **静态源**（如 OpenClaw 财务 `:18765/finance`）：`{id, name, cards: [...]}` —— 卡片在配置里直接声明，**门户不抓取**。适合不暴露 manifest 的单页看板，尤其是**在另一台 tailnet 主机上**的：卡片链接由用户**浏览器**打开（浏览器在 tailnet 内可达），所以门户容器不需要能够到那台主机。卡片 `url` 写绝对地址即原样用，相对路径才拼 `link_base`。

> 任何源要接入门户实现的那个 `/portal_manifest` 端点的完整 JSON 规范，见
> [docs/MANIFEST-CONTRACT.md](docs/MANIFEST-CONTRACT.md)。

## 部署

```sh
deploy/redeploy.sh
```
同步源码到 NAS `/volume1/docker/report-portal-src/` → build → `compose -p report-portal up` → 探活。

网络：compose 把容器挂到 `kg-hub_default`（external），从而能用服务名 `kg_hub_server` 抓 manifest；门户自身发布在 `127.0.0.1:17172`。

## 验证

```sh
curl -s -o /dev/null -w "health=%{http_code}\n" http://100.123.208.32:17172/health
curl -s -o /dev/null -w "portal=%{http_code}\n" http://100.123.208.32:17172/portal
curl -s http://100.123.208.32:17172/portal | grep -oE "知识胶囊看板|使用排行"   # 应能看到 kg-hub 的卡片
```

## 一个源不可达不会拖垮门户

`portal.py` 抓每个源的 manifest 都 try/except；失败的源显示红点 + "暂不可达"，其它源照常渲染。
