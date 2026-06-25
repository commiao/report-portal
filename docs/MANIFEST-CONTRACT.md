# Portal Manifest 契约

任何想出现在报表门户(`report-portal`)里的数据源,只需实现这一个只读端点。
门户**不读你的库**——它只抓这份 manifest,把你声明的卡片渲染进统一首页。

## 端点

```
GET <fetch_base>/portal_manifest        # 路径可在源配置里改，默认 /portal_manifest
```

- **只读、免鉴权**:门户服务端抓取,不带 token。端点只返回卡片元数据(名称/描述/链接),
  无敏感数据,所以应放行(kg-hub 是把它纳入 `/portal*` 的鉴权放行清单)。
- **必须 200 + JSON**。任何错误/超时 → 门户把该源标红"暂不可达",不影响其它源。

## 响应体

```json
{
  "source": "kg-hub",
  "reports": [
    {
      "name": "知识胶囊看板",
      "desc": "canonical 胶囊曝光 + 各 cwd 下实时排序与注入",
      "url": "/dashboard/capsules",
      "icon": "📎",
      "ready": true
    }
  ]
}
```

| 字段 | 必填 | 说明 |
|---|---|---|
| `source` | 否 | 源标识,展示用。缺省回退到门户配置里的 `id`。 |
| `reports` | 是 | 卡片数组。也兼容直接返回一个数组(无 `reports` 包裹)。 |
| `reports[].name` | 是 | 卡片标题。 |
| `reports[].desc` | 否 | 一行描述。 |
| `reports[].url` | 是 | 看板地址。**用相对路径**(如 `/dashboard/x`);门户会拼成 `link_base + url` 成为浏览器可点的绝对地址。已是绝对 URL 时原样使用。 |
| `reports[].icon` | 否 | emoji,缺省 `📄`。 |
| `reports[].ready` | 否 | `false` 时卡片置灰显示"即将上线"且不可点。缺省 `true`。 |

## 两个 base(为什么 url 用相对路径)

门户和用户浏览器处于不同网络位置,所以每个源在门户侧配两个 base:

- `fetch_base`:**门户容器**可达,用于服务端抓 manifest(NAS 上走共享 docker 网络,
  如 `http://kg_hub_server:8080`)。
- `link_base`:**用户浏览器**可达(tailnet 地址,如 `http://100.123.208.32:17171`),
  用于渲染可点击的卡片链接。

manifest 里的相对 `url` 由门户用 `link_base` 拼成绝对地址,所以**你的源不必知道自己的对外地址**。

## 接入清单

1. 在你的服务里实现 `GET /portal_manifest`,返回上面的 JSON。
2. 确保它免 token 可被门户抓到(放行 / 同网络可达)。
3. 在 report-portal 的 `PORTAL_SOURCES` 加一条 `{id, name, fetch_base, link_base, manifest}`。
4. `deploy/redeploy.sh` 重启门户即可——**门户代码不用改**。

## 关键边界

**看板留在源那边自己渲染**(它紧挨自己的数据),门户只做聚合与导航。
不要把看板的取数/渲染搬进门户——那会把已经拆掉的耦合用 API 表面重新造出来。
详见 [README](../README.md)。
