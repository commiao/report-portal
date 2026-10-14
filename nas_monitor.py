#!/usr/bin/env python3
"""nas_monitor — NAS 存储与写盘监控（report-portal 的一个数据源）。

为什么有它（2026-10-14）：NAS 写盘治理（T-0234/T-0245）把低负载写入从约 6.3MB/s 降到
约 0.8MB/s，但一路上的问题都是「事后才发现」：账本涨到 19.2GB、FalkorDB 每 8 分钟重写
456MB、1015 个镜像里 97% 可回收、每次发布留下 3GB 离线包。这里把它们变成持续可见的数字，
按 monitor/projects.json 里登记的预算打 ok/warn/crit，越线时发飞书。

它能看到什么、看不到什么：
  - 整机读写速度：/proc/diskstats（md 设备，不分进程）。
  - 各项目的存储：只读挂载的数据目录，按登记的路径统计占用、日增长、残留个数。
  - 容器与镜像：只读访问 docker API（只发 GET），取日志驱动、健康检查间隔、镜像标签、
    悬空镜像、构建缓存、容器创建/启动事件（用来标注发布和实验造成的写盘尖峰）。
  - **看不到按进程/按容器的写盘量**：那需要 --pid=host 或 root 读 /proc/*/io，
    按约定不用特权绕过（见 fleet-ops 记忆 proc-io-misses-forked-children）。

它自己也写盘：一个常驻 SQLite 连接，每 FLUSH_SECONDS 批量提交一次。按 2026-10-13 在这台
NAS 上测得的单价（一次提交约 0.4–1.4MB 设备写入），平均约 1–5KB/s。
"""
from __future__ import annotations

import base64
import collections
import hashlib
import hmac
import html
import json
import logging
import os
import re
import sqlite3
import statistics
import threading
import time
from pathlib import Path
from typing import Any, Callable

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

log = logging.getLogger("nas_monitor")

REGISTRY_PATH = Path(os.environ.get(
    "NAS_MONITOR_REGISTRY", str(Path(__file__).resolve().with_name("monitor") / "projects.json")))
DB_PATH = Path(os.environ.get("NAS_MONITOR_DB", "/data/nas-monitor.sqlite3"))
DISKSTATS_PATH = os.environ.get("NAS_MONITOR_DISKSTATS", "/proc/diskstats")
DOCKER_SOCKET = os.environ.get("NAS_MONITOR_DOCKER_SOCKET", "/var/run/docker.sock")
FEISHU_FILE = os.environ.get("NAS_MONITOR_FEISHU_FILE", "")
DASHBOARD_PATH = "/dashboard/nas"

SAMPLE_SECONDS = 10
MINUTE = 60
FLUSH_SECONDS = 300
DOCKER_SECONDS = 300
SCAN_SECONDS = 1800
DF_SECONDS = 7200          # /system/df 在这台 NAS 上要 ~2 分钟（逐层算大小），2 小时一次
RETENTION_DAYS = 30
WARMUP_SECONDS = 600
CRIT_RESEND_SECONDS = 12 * 3600
GiB = 1024 ** 3
MiB = 1024 ** 2

LEVELS = {"ok": 0, "unknown": 0, "warn": 1, "crit": 2}


# ---------------------------------------------------------------------------
# 采集：磁盘计数
# ---------------------------------------------------------------------------
def parse_diskstats(text: str, devices: list[str]) -> dict[str, tuple[int, int, int, int]]:
    """{dev: (reads, sectors_read, writes, sectors_written)}；只取登记的设备。"""
    out = {}
    for line in text.splitlines():
        f = line.split()
        if len(f) >= 10 and f[2] in devices:
            out[f[2]] = (int(f[3]), int(f[5]), int(f[7]), int(f[9]))
    return out


def minute_rates(prev: dict, cur: dict, seconds: float) -> dict[str, dict[str, float]]:
    """两次计数之差 → 每秒速率（KB/s、次/s）。计数回绕或设备消失时跳过。"""
    out = {}
    for dev, (r, rs, w, ws) in cur.items():
        if dev not in prev or seconds <= 0:
            continue
        pr, prs, pw, pws = prev[dev]
        if r < pr or rs < prs or w < pw or ws < pws:
            continue
        out[dev] = {
            "rkb": (rs - prs) * 512 / 1024 / seconds,
            "rops": (r - pr) / seconds,
            "wkb": (ws - pws) * 512 / 1024 / seconds,
            "wops": (w - pw) / seconds,
        }
    return out


# ---------------------------------------------------------------------------
# 采集：目录占用
# ---------------------------------------------------------------------------
def disk_usage(path: str) -> tuple[int, int] | None:
    """(实际占用字节, 文件数)。不跟随符号链接；读不到的子项跳过。路径不存在返回 None。"""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if not os.path.isdir(path) or os.path.islink(path):
        return st.st_blocks * 512, 1
    total, files, stack = 0, 0, [path]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as entries:
                for entry in entries:
                    try:
                        est = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    total += est.st_blocks * 512
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                    else:
                        files += 1
        except OSError:
            continue
    return total, files


def scan_children(spec: dict, now: float) -> dict | None:
    """登记的「同类子项」（发布包、备份、残留）：个数、总占用、最老的年龄。"""
    try:
        names = sorted(n for n in os.listdir(spec["dir"]) if re.search(spec["pattern"], n))
    except OSError:
        return None
    total, oldest = 0, None
    for name in names:
        full = os.path.join(spec["dir"], name)
        usage = disk_usage(full)
        if usage:
            total += usage[0]
        try:
            age_h = (now - os.lstat(full).st_mtime) / 3600
            oldest = age_h if oldest is None else max(oldest, age_h)
        except OSError:
            pass
    return {"count": len(names), "bytes": total, "oldest_age_h": oldest, "names": names[-12:]}


def file_age_hours(spec: dict, now: float) -> float | None:
    """新鲜度：优先读 JSON 里的时间字段（ISO 8601），否则用文件 mtime。"""
    path = spec["path"]
    field = spec.get("json_time_field")
    try:
        if field:
            from datetime import datetime
            raw = json.loads(Path(path).read_text("utf-8")).get(field)
            return (now - datetime.fromisoformat(str(raw)).timestamp()) / 3600
        return (now - os.lstat(path).st_mtime) / 3600
    except (OSError, ValueError, TypeError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# 采集：docker（只读 GET）
# ---------------------------------------------------------------------------
class DockerReader:
    """只发 GET。容器详情里只取日志驱动和健康检查间隔，环境变量等一概不读出来。"""

    def __init__(self, socket_path: str, timeout: float = 120.0):
        self._client = httpx.Client(transport=httpx.HTTPTransport(uds=socket_path),
                                    base_url="http://docker", timeout=timeout)

    def _get(self, path: str, **params) -> Any:
        resp = self._client.get(path, params=params)
        resp.raise_for_status()
        return resp.json()

    def containers(self) -> list[dict]:
        return self._get("/containers/json", all="1")

    def container_policy(self, cid: str) -> dict:
        data = self._get(f"/containers/{cid}/json")
        return policy_fields(data)

    def system_df(self) -> dict:
        return self._get("/system/df")

    def events(self, since: int, until: int) -> list[dict]:
        filters = json.dumps({"type": ["container"], "event": ["create", "start"]})
        resp = self._client.get("/events", params={"since": str(since), "until": str(until),
                                                   "filters": filters})
        resp.raise_for_status()
        return [json.loads(line) for line in resp.text.splitlines() if line.strip()]


def policy_fields(inspect: dict) -> dict:
    """从 docker inspect 里只挑两项。单独成函数，是为了能测「别的字段一个都不留」。"""
    log_driver = ((inspect.get("HostConfig") or {}).get("LogConfig") or {}).get("Type")
    health = (inspect.get("Config") or {}).get("Healthcheck") or {}
    interval_ns = health.get("Interval")
    has_check = bool(health.get("Test")) and health.get("Test") != ["NONE"]
    interval_s = (interval_ns / 1e9 if interval_ns else 30.0) if has_check else None
    return {"log_driver": log_driver, "healthcheck_interval_s": interval_s}


def image_summary(df: dict, registry: dict) -> dict:
    """/system/df → 可回收空间、悬空镜像、构建缓存、各登记仓库的标签数。

    可回收空间按 docker CLI（`docker system df`）的算法：全部层的大小，减去有容器在用的
    镜像的独占部分。悬空镜像之间大量共享层，按「每个镜像独占」相加会严重低估（实测
    1.7GB 对 14.4GB），所以悬空镜像只报个数和独占部分，标为「至少」。
    """
    images = df.get("Images") or []
    layers = int(df.get("LayersSize") or 0)
    dangling_bytes, dangling_count, used = 0, 0, 0
    tags: dict[str, int] = collections.Counter()
    for img in images:
        size = int(img.get("Size") or 0)
        shared = int(img.get("SharedSize") or 0)
        unique = max(size - max(shared, 0), 0)
        repo_tags = [t for t in (img.get("RepoTags") or []) if t and t != "<none>:<none>"]
        if not repo_tags:
            dangling_count += 1
            dangling_bytes += unique
        if int(img.get("Containers") or 0) > 0 and size >= 0 and shared >= 0:
            used += size - shared
        for tag in repo_tags:
            tags[tag.rsplit(":", 1)[0]] += 1
    total, reclaim = layers, max(layers - used, 0)
    cache = sum(int(c.get("Size") or 0) for c in (df.get("BuildCache") or []))
    repos = {repo: tags.get(repo, 0)
             for project in registry["projects"] for repo in project.get("image_repos", [])}
    return {"image_count": len(images), "images_bytes": total, "reclaimable_bytes": reclaim,
            "dangling_count": dangling_count, "dangling_bytes": dangling_bytes,
            "build_cache_bytes": cache, "repo_tags": repos,
            "layers_bytes": layers}


def project_of(registry: dict, name: str, labels: dict) -> str:
    """容器/事件归属：登记项目 → 实验 → 临时（构建等）→ 其他。"""
    compose = (labels or {}).get("com.docker.compose.project", "")
    for p in registry["projects"]:
        if compose and compose in p.get("compose_projects", []):
            return p["id"]
        if name in p.get("containers", []):
            return p["id"]
    for pattern in registry.get("adhoc_container_patterns", []):
        if re.search(pattern, name):
            return "experiments"
    if not compose and re.fullmatch(r"[a-z]+_[a-z]+", name or ""):
        return "temporary"
    return "other"


# ---------------------------------------------------------------------------
# 检查（纯函数：输入快照，输出发现；方便测试）
# ---------------------------------------------------------------------------
def _level(value: float | None, warn: float, crit: float) -> str:
    if value is None:
        return "unknown"
    return "crit" if value >= crit else "warn" if value >= warn else "ok"


def _gb(b: float | None) -> str:
    return "—" if b is None else f"{b / GiB:.2f} GB"


def evaluate(registry: dict, snap: dict) -> list[dict]:
    g = registry["global"]
    out: list[dict] = []

    def add(project, key, label, level, value, budget):
        out.append({"project": project, "key": f"{project}:{key}", "label": label,
                    "level": level, "value": value, "budget": budget})

    # 整机写入：1 小时内分钟速率的中位数（不被几分钟的发布尖峰带偏）
    minutes = snap.get("write_minutes_1h") or []
    median = statistics.median(minutes) if len(minutes) >= 30 else None
    b = g["write_kbs_1h_median"]
    add("global", "write-rate", "整机写入（1 小时中位数，md2+md3）",
        _level(median, b["warn"], b["crit"]),
        "数据不足 30 分钟" if median is None else f"{median:.0f} KB/s",
        f"warn ≥ {b['warn']} / crit ≥ {b['crit']} KB/s")

    b = g["volume_used_pct"]
    for dev, vol in sorted((snap.get("volumes") or {}).items()):
        pct = vol.get("used_pct")
        add("global", f"volume-{dev}", f"{vol.get('label', dev)} 已用空间",
            _level(pct, b["warn"], b["crit"]),
            "—" if pct is None else f"{pct:.0f}%（{_gb(vol.get('used'))} / {_gb(vol.get('total'))}）",
            f"warn ≥ {b['warn']}% / crit ≥ {b['crit']}%")

    img = snap.get("images")
    b = g["dangling_images_gb"]
    add("global", "dangling-images", "悬空镜像（<none>）",
        _level(None if img is None else img["dangling_bytes"] / GiB, b["warn"], b["crit"]),
        "—" if img is None else f"{img['dangling_count']} 个，独占至少 {_gb(img['dangling_bytes'])}",
        f"warn ≥ {b['warn']} / crit ≥ {b['crit']} GB")
    b = g["build_cache_gb"]
    add("global", "build-cache", "构建缓存",
        _level(None if img is None else img["build_cache_bytes"] / GiB, b["warn"], b["crit"]),
        "—" if img is None else _gb(img["build_cache_bytes"]),
        f"warn ≥ {b['warn']} / crit ≥ {b['crit']} GB")
    b = g["exited_containers"]
    exited = snap.get("exited_unowned")
    add("global", "exited-containers", "已退出的临时/实验容器",
        _level(exited, b["warn"], b["crit"]), "—" if exited is None else f"{exited} 个",
        f"warn ≥ {b['warn']} / crit ≥ {b['crit']}")

    paths = snap.get("paths") or {}
    growth = snap.get("growth") or {}
    children = snap.get("children") or {}
    fresh = snap.get("freshness") or {}
    policies = snap.get("policies") or {}
    for p in registry["projects"]:
        pid = p["id"]
        for spec in p.get("paths", []):
            size = paths.get((pid, spec["id"]))
            gb = None if size is None else size / GiB
            add(pid, f"path-{spec['id']}", spec["label"],
                "unknown" if gb is None else _level(gb, spec["warn_gb"], spec["crit_gb"]),
                "路径不存在或读不到" if size is None else _gb(size),
                f"warn ≥ {spec['warn_gb']} / crit ≥ {spec['crit_gb']} GB")
            per_day = growth.get((pid, spec["id"]))
            limit = spec.get("max_growth_mb_per_day")
            if limit:
                add(pid, f"growth-{spec['id']}", f"{spec['label']} 日增长",
                    _level(per_day, limit, limit * 2),
                    "历史不足 6 小时" if per_day is None else f"{per_day:+.0f} MB/天",
                    f"warn ≥ {limit} / crit ≥ {limit * 2} MB/天")
        for spec in p.get("children", []):
            c = children.get((pid, spec["id"]))
            if c is None:
                add(pid, f"children-{spec['id']}", spec["label"], "unknown", "目录读不到",
                    f"保留 ≤ {spec['keep']} 个")
                continue
            over_count = c["count"] - spec["keep"]
            over_bytes = c["bytes"] / GiB - spec["max_total_gb"]
            level = "ok"
            if over_count > 0 or over_bytes > 0:
                level = "warn"
            if over_count > max(spec["keep"], 2) or c["bytes"] / GiB > 2 * spec["max_total_gb"] + 0.5:
                level = "crit"
            max_age = spec.get("max_age_hours")
            if max_age is not None and c["count"] and (c["oldest_age_h"] or 0) > max_age:
                level = max(level, "warn", key=LEVELS.get)
            add(pid, f"children-{spec['id']}", spec["label"], level,
                f"{c['count']} 个，{_gb(c['bytes'])}",
                f"保留 ≤ {spec['keep']} 个、≤ {spec['max_total_gb']} GB"
                + (f"、存在不超过 {max_age} 小时" if max_age is not None else ""))
        for spec in p.get("freshness", []):
            age = fresh.get((pid, spec["id"]))
            limit = spec["max_age_hours"]
            add(pid, f"fresh-{spec['id']}", spec["label"],
                "warn" if age is None else _level(age, limit, limit * 2),
                "读不到" if age is None else f"{age:.1f} 小时前",
                f"≤ {limit} 小时")
        repos = (img or {}).get("repo_tags") or {}
        for repo in p.get("image_repos", []):
            n = repos.get(repo) if img else None
            keep = p.get("max_image_tags", 5)
            add(pid, f"image-tags-{repo}", f"镜像版本 {repo}",
                _level(n, keep + 1, keep * 4), "—" if n is None else f"{n} 个标签",
                f"保留 ≤ {keep} 个")
        rule = p.get("container_policy") or {}
        bad = []
        for name, pol in sorted((policies.get(pid) or {}).items()):
            interval = pol.get("healthcheck_interval_s")
            if interval is not None and interval < rule.get("min_healthcheck_interval_s", 0):
                bad.append(f"{name} 健康检查每 {interval:.0f}s")
            if pol.get("log_driver") in rule.get("forbidden_log_drivers", []):
                bad.append(f"{name} 日志驱动 {pol['log_driver']}")
        if rule:
            add(pid, "container-policy", "容器写盘配置（健康检查间隔、日志驱动）",
                "unknown" if pid not in policies else ("warn" if bad else "ok"),
                "；".join(bad) if bad else ("—" if pid not in policies else f"{len(policies[pid])} 个容器合规"),
                f"健康检查 ≥ {rule.get('min_healthcheck_interval_s', 0)}s；不用 "
                + "/".join(rule.get("forbidden_log_drivers", [])) + " 日志驱动")
    return out


# ---------------------------------------------------------------------------
# 告警：只在升级、恢复、以及 crit 每 12 小时时发
# ---------------------------------------------------------------------------
def alert_transitions(findings: list[dict], state: dict[str, dict], now: float) -> tuple[list[str], dict]:
    """返回 (要发的行, 新状态)。state: key → {level, since, last_sent}。

    只在三种时候发：升级（ok→warn、warn→crit 等）、crit 持续满 12 小时、从 warn/crit 恢复。
    读不到数据（unknown）不算恢复也不算升级，沿用上一次的状态。
    """
    lines, new_state = [], {}
    for f in findings:
        key, level = f["key"], f["level"]
        prev = state.get(key)
        if level == "unknown":
            if prev:
                new_state[key] = prev
            continue
        record = dict(prev) if prev else {"level": "ok", "since": now, "last_sent": 0}
        if level == "ok":
            if record["level"] in ("warn", "crit"):
                lines.append(f"✅ 已恢复 [{f['project']}] {f['label']}：{f['value']}")
                record.update(level="ok", since=now)
            new_state[key] = record
            continue
        escalated = LEVELS[level] > LEVELS[record["level"]]
        repeat = level == "crit" and now - record["last_sent"] >= CRIT_RESEND_SECONDS
        if escalated or repeat:
            mark = "🔴" if level == "crit" else "🟡"
            lines.append(f"{mark} [{f['project']}] {f['label']}：{f['value']}（预算 {f['budget']}）")
            record["last_sent"] = now
        if level != record["level"]:
            record["since"] = now
        record["level"] = level
        new_state[key] = record
    return lines, new_state


def feishu_sender(path: str) -> Callable[[str], bool] | None:
    """读 {"url": ..., "secret"?: ...}（0600 文件，不进 git）。没配置就不发。"""
    if not path:
        return None
    try:
        cfg = json.loads(Path(path).read_text("utf-8"))
        url, secret = cfg["url"], cfg.get("secret")
    except (OSError, ValueError, KeyError, TypeError):
        log.warning("飞书配置读不到，告警只显示在看板上")
        return None

    def send(text: str) -> bool:
        body: dict[str, Any] = {"msg_type": "text", "content": {"text": text}}
        if secret:
            ts = str(int(time.time()))
            sign = hmac.new(f"{ts}\n{secret}".encode(), b"", hashlib.sha256).digest()
            body.update(timestamp=ts, sign=base64.b64encode(sign).decode())
        try:
            r = httpx.post(url, json=body, timeout=10)
            return r.status_code == 200 and r.json().get("code", r.json().get("StatusCode")) == 0
        except (httpx.HTTPError, ValueError):
            return False
    return send


# ---------------------------------------------------------------------------
# 存储（一个常驻连接，批量提交）
# ---------------------------------------------------------------------------
class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS io(ts INTEGER, dev TEXT, wkb REAL, wops REAL, rkb REAL, rops REAL);
            CREATE INDEX IF NOT EXISTS io_ts ON io(ts);
            CREATE TABLE IF NOT EXISTS usage(ts INTEGER, project TEXT, item TEXT, bytes INTEGER, count INTEGER);
            CREATE INDEX IF NOT EXISTS usage_key ON usage(project, item, ts);
            CREATE TABLE IF NOT EXISTS events(ts INTEGER, project TEXT, kind TEXT, name TEXT);
            CREATE INDEX IF NOT EXISTS events_ts ON events(ts);
            CREATE TABLE IF NOT EXISTS alert_state(key TEXT PRIMARY KEY, level TEXT, since REAL, last_sent REAL);
        """)
        self.lock = threading.Lock()

    def write(self, io_rows=(), usage_rows=(), event_rows=()):
        with self.lock:
            self.db.execute("BEGIN")
            self.db.executemany("INSERT INTO io VALUES(?,?,?,?,?,?)", io_rows)
            self.db.executemany("INSERT INTO usage VALUES(?,?,?,?,?)", usage_rows)
            self.db.executemany("INSERT INTO events VALUES(?,?,?,?)", event_rows)
            self.db.execute("COMMIT")

    def prune(self, now: float):
        cutoff = int(now - RETENTION_DAYS * 86400)
        with self.lock:
            self.db.execute("BEGIN")
            for table in ("io", "usage", "events"):
                self.db.execute(f"DELETE FROM {table} WHERE ts < ?", (cutoff,))
            self.db.execute("COMMIT")

    def query(self, sql: str, args=()) -> list[tuple]:
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def load_alert_state(self) -> dict:
        return {k: {"level": lv, "since": s, "last_sent": ls}
                for k, lv, s, ls in self.query("SELECT key, level, since, last_sent FROM alert_state")}

    def save_alert_state(self, state: dict):
        with self.lock:
            self.db.execute("BEGIN")
            self.db.execute("DELETE FROM alert_state")
            self.db.executemany("INSERT INTO alert_state VALUES(?,?,?,?)",
                                [(k, v["level"], v["since"], v["last_sent"]) for k, v in state.items()])
            self.db.execute("COMMIT")


def growth_per_day(store: Store, project: str, item: str, now: float, current: int) -> float | None:
    """MB/天：与约 24 小时前（至少 6 小时前）的那次扫描比较。"""
    rows = store.query("SELECT ts, bytes FROM usage WHERE project=? AND item=? AND ts <= ? "
                       "ORDER BY ABS(ts - ?) LIMIT 1",
                       (project, item, int(now - 6 * 3600), int(now - 86400)))
    if not rows or rows[0][1] is None:
        return None
    ts, before = rows[0]
    days = (now - ts) / 86400
    return (current - before) / MiB / days if days > 0 else None


# ---------------------------------------------------------------------------
# 监控主体
# ---------------------------------------------------------------------------
class Monitor:
    def __init__(self, registry: dict, store: Store, docker: DockerReader | None,
                 sender: Callable[[str], bool] | None, diskstats_path: str = DISKSTATS_PATH):
        self.registry, self.store, self.docker, self.sender = registry, store, docker, sender
        self.diskstats_path = diskstats_path
        self.devices = list(registry["devices"])
        self.started = time.time()
        self.lock = threading.Lock()
        self.recent = collections.deque(maxlen=24 * 60 * len(self.devices))  # 内存里留 24h 分钟数据
        self.pending_io: list[tuple] = []
        self.pending_usage: list[tuple] = []
        self.pending_events: list[tuple] = []
        self.snapshot: dict = {}
        self.findings: list[dict] = []
        self.last = {"sample": 0.0, "docker": 0.0, "scan": 0.0, "flush": 0.0, "evaluate": 0.0}
        self.errors: dict[str, str] = {}
        self.alert_state = store.load_alert_state()
        self._warm_from_store()

    def _warm_from_store(self):
        since = int(time.time() - 86400)
        for row in self.store.query("SELECT ts, dev, wkb, wops, rkb, rops FROM io WHERE ts >= ? ORDER BY ts", (since,)):
            self.recent.append(row)

    # -- 采样 --------------------------------------------------------------
    def read_counters(self) -> dict:
        return parse_diskstats(Path(self.diskstats_path).read_text(), self.devices)

    def record_minute(self, ts: int, rates: dict):
        rows = [(ts, dev, r["wkb"], r["wops"], r["rkb"], r["rops"]) for dev, r in sorted(rates.items())]
        with self.lock:
            self.recent.extend(rows)
            self.pending_io.extend(rows)
        self.last["sample"] = time.time()

    def write_minutes(self, seconds: int = 3600) -> list[float]:
        """每分钟整机写入（所有登记设备相加）。"""
        cutoff = time.time() - seconds
        per_minute: dict[int, float] = collections.defaultdict(float)
        with self.lock:
            for ts, _dev, wkb, *_ in self.recent:
                if ts >= cutoff:
                    per_minute[ts] += wkb
        return [per_minute[k] for k in sorted(per_minute)]

    # -- 存储与容器 --------------------------------------------------------
    def scan_storage(self, now: float):
        paths, growth, children, fresh, usage_rows = {}, {}, {}, {}, []
        for p in self.registry["projects"]:
            pid = p["id"]
            for spec in p.get("paths", []):
                u = disk_usage(spec["path"])
                paths[(pid, spec["id"])] = None if u is None else u[0]
                if u is not None:
                    growth[(pid, spec["id"])] = growth_per_day(self.store, pid, spec["id"], now, u[0])
                    usage_rows.append((int(now), pid, spec["id"], u[0], u[1]))
            for spec in p.get("children", []):
                c = scan_children(spec, now)
                children[(pid, spec["id"])] = c
                if c is not None:
                    usage_rows.append((int(now), pid, "children:" + spec["id"], c["bytes"], c["count"]))
            for spec in p.get("freshness", []):
                fresh[(pid, spec["id"])] = file_age_hours(spec, now)
        volumes = {}
        for dev, spec in self.registry["devices"].items():
            try:
                st = os.statvfs(spec["statvfs_path"])
                total, free = st.f_blocks * st.f_frsize, st.f_bavail * st.f_frsize
                volumes[dev] = {"label": spec["label"], "total": total, "used": total - free,
                                "used_pct": 100 * (total - free) / total if total else None}
            except OSError:
                volumes[dev] = {"label": spec["label"], "used_pct": None}
        with self.lock:
            self.snapshot.update(paths=paths, growth=growth, children=children,
                                 freshness=fresh, volumes=volumes)
            self.pending_usage.extend(usage_rows)
        self.last["scan"] = now

    def refresh_docker(self, now: float, with_df: bool):
        if self.docker is None:
            return
        containers = self.docker.containers()
        policies: dict[str, dict] = collections.defaultdict(dict)
        exited_unowned = 0
        for c in containers:
            name = (c.get("Names") or ["/?"])[0].lstrip("/")
            project = project_of(self.registry, name, c.get("Labels") or {})
            if c.get("State") == "running" and project in {p["id"] for p in self.registry["projects"]}:
                policies[project][name] = self.docker.container_policy(c["Id"])
            elif c.get("State") == "exited" and project in ("experiments", "temporary", "other"):
                exited_unowned += 1
        since = int(self.last["docker"] or now - DOCKER_SECONDS)
        events = []
        for e in self.docker.events(since, int(now)):
            attrs = (e.get("Actor") or {}).get("Attributes") or {}
            name = attrs.get("name", "")
            events.append((int(e.get("time", now)), project_of(self.registry, name, attrs),
                           e.get("Action", ""), name))
        update = {"policies": dict(policies), "exited_unowned": exited_unowned}
        if with_df:
            update["images"] = image_summary(self.docker.system_df(), self.registry)
        with self.lock:
            self.snapshot.update(update)
            self.pending_events.extend(events)
        self.last["docker"] = now

    # -- 检查、告警、落盘 ----------------------------------------------------
    def evaluate_and_alert(self, now: float):
        with self.lock:
            snap = dict(self.snapshot)
        snap["write_minutes_1h"] = self.write_minutes(3600)
        findings = evaluate(self.registry, snap)
        lines, new_state = alert_transitions(findings, self.alert_state, now)
        # 只有真正送达才推进告警状态：没配飞书或发送失败时保持原状，下一轮再发。
        # 否则「没配 webhook 期间」的越线会被记成已通知，配好之后一条也不会来。
        delivered = not lines
        if lines and self.sender:
            text = "NAS 存储与写盘监控\n" + "\n".join(lines) + f"\n看板：{os.environ.get('NAS_MONITOR_LINK', '')}{DASHBOARD_PATH}"
            delivered = self.sender(text)
            if delivered:
                self.errors.pop("feishu", None)
            else:
                self.errors["feishu"] = time.strftime("%F %T") + " 飞书发送失败，下一轮重试"
        if delivered:
            self.alert_state = new_state
        with self.lock:
            self.findings = findings
        self.store.save_alert_state(self.alert_state)
        self.last["evaluate"] = now

    def flush(self, now: float):
        with self.lock:
            io_rows, usage_rows, event_rows = self.pending_io, self.pending_usage, self.pending_events
            self.pending_io, self.pending_usage, self.pending_events = [], [], []
        if io_rows or usage_rows or event_rows:
            self.store.write(io_rows, usage_rows, event_rows)
        self.last["flush"] = now

    def run_forever(self, stop: threading.Event):
        """两个线程：采样线程只读计数、按分钟记账、定时落盘，不能被慢任务卡住；
        慢任务（docker、目录扫描、检查）放在另一个线程——/system/df 一次要 ~2 分钟。"""
        threading.Thread(target=self._slow_loop, args=(stop,), daemon=True, name="slow").start()
        self._sample_loop(stop)

    def _sample_loop(self, stop: threading.Event):
        minute_start = None
        next_minute = (int(time.time()) // MINUTE + 1) * MINUTE
        last_prune = 0.0
        while not stop.is_set():
            now = time.time()
            try:
                cur, cur_t = self.read_counters(), time.monotonic()
                if minute_start is None:
                    minute_start = (cur, cur_t)
                if now >= next_minute:
                    start_counts, start_t = minute_start
                    rates = minute_rates(start_counts, cur, cur_t - start_t)
                    if rates:
                        self.record_minute(next_minute - MINUTE, rates)
                    minute_start = (cur, cur_t)
                    next_minute = (int(now) // MINUTE + 1) * MINUTE
                self.errors.pop("diskstats", None)
            except OSError as exc:
                self.errors["diskstats"] = str(exc)
            if now - self.last["flush"] >= FLUSH_SECONDS:
                try:
                    self.flush(now)
                    if now - last_prune >= 86400:
                        self.store.prune(now)
                        last_prune = now
                    self.errors.pop("store", None)
                except sqlite3.Error as exc:
                    self.errors["store"] = str(exc)
            stop.wait(SAMPLE_SECONDS)

    def _slow_loop(self, stop: threading.Event):
        last_df = 0.0
        while not stop.is_set():
            now = time.time()
            if now - self.last["scan"] >= SCAN_SECONDS:
                try:
                    self.scan_storage(now)
                    self.errors.pop("scan", None)
                except Exception as exc:  # noqa: BLE001 — 一项失败不拖垮整个采集
                    self.errors["scan"] = f"{type(exc).__name__}: {exc}"
                    self.last["scan"] = now
            if now - self.last["docker"] >= DOCKER_SECONDS:
                with_df = now - last_df >= DF_SECONDS
                try:
                    self.refresh_docker(now, with_df=with_df)
                    if with_df:
                        last_df = now
                    self.errors.pop("docker", None)
                except Exception as exc:  # noqa: BLE001
                    self.errors["docker"] = f"{type(exc).__name__}: {exc}"
                    self.last["docker"] = now
            if now - self.last["evaluate"] >= DOCKER_SECONDS:
                try:
                    self.evaluate_and_alert(time.time())
                    self.errors.pop("evaluate", None)
                except Exception as exc:  # noqa: BLE001
                    self.errors["evaluate"] = f"{type(exc).__name__}: {exc}"
            stop.wait(SAMPLE_SECONDS)

    # -- 对外数据 ------------------------------------------------------------
    def health(self, now: float) -> tuple[int, dict]:
        """量的是「采集还在干活」，不是「进程还活着」。

        - 磁盘采样超过 3 分钟没更新 → 503（启动后头 90 秒还没第一次采样不算）。
        - 启动 10 分钟后，存储扫描或 docker 采集长期没更新也 → 503。
        - 检查结果有 warn/crit 不影响 health：那是它如实上报的数据，不是它坏了，
          不能让 release.sh 因为 NAS 上有残留就把监控自己回滚掉。
        """
        uptime = now - self.started
        stale = []
        if not self.last["sample"]:
            if uptime >= 90:
                stale.append("磁盘采样")
        elif now - self.last["sample"] > 180:
            stale.append("磁盘采样")
        warming = uptime <= WARMUP_SECONDS
        if not warming:
            if not self.last["scan"] or now - self.last["scan"] > SCAN_SECONDS * 2.5:
                stale.append("存储扫描")
            if self.docker and (not self.last["docker"] or now - self.last["docker"] > DOCKER_SECONDS * 3):
                stale.append("docker 采集")
        if stale:
            return 503, {"status": "stale", "stale": stale, "errors": self.errors}
        worst = max((LEVELS[f["level"]] for f in self.findings), default=0)
        return 200, {"status": "warming" if warming else "ok", "worst": ["ok", "warn", "crit"][worst],
                     "findings": len(self.findings), "errors": self.errors}

    def status(self, now: float) -> dict:
        since = int(now - 86400)
        buckets: dict[tuple[int, str], list] = collections.defaultdict(list)
        with self.lock:
            rows = list(self.recent)
            findings = list(self.findings)
            snap = dict(self.snapshot)
        for ts, dev, wkb, wops, rkb, rops in rows:
            if ts >= since:
                buckets[(ts // 300 * 300, dev)].append((wkb, wops, rkb, rops))
        series = collections.defaultdict(list)
        for (bts, dev), vals in sorted(buckets.items()):
            n = len(vals)
            series[dev].append([bts, sum(v[0] for v in vals) / n, sum(v[1] for v in vals) / n,
                                sum(v[2] for v in vals) / n, sum(v[3] for v in vals) / n])
        events = self.store.query("SELECT ts, project, kind, name FROM events WHERE ts >= ? ORDER BY ts", (since,))
        with self.lock:
            events += [e for e in self.pending_events if e[0] >= since]
        minutes = self.write_minutes(3600)
        return {
            "generated_at": now,
            "devices": self.registry["devices"],
            "series": series,
            "events": sorted(set(events)),
            "findings": findings,
            # 与检查口径一致：不满 30 分钟不给中位数，免得刚启动时拿几分钟的数冒充 1 小时
            "write_1h_median": statistics.median(minutes) if len(minutes) >= 30 else None,
            "write_minutes": len(minutes),
            "write_last_minute": minutes[-1] if minutes else None,
            "volumes": snap.get("volumes", {}),
            "images": snap.get("images"),
            "children": {f"{k[0]}:{k[1]}": v for k, v in (snap.get("children") or {}).items()},
            "projects": [{"id": p["id"], "name": p["name"]} for p in self.registry["projects"]],
            "last": self.last, "errors": self.errors,
        }


# ---------------------------------------------------------------------------
# 页面
# ---------------------------------------------------------------------------
PROJECT_COLORS = {"kg-hub": "#2a78d6", "credvault": "#1baf7a", "zhaopin": "#d88c1b",
                  "experiments": "#9a5bd6", "temporary": "#8a8a8a", "other": "#b0b0b0"}
PROJECT_LABELS = {"global": "整机", "experiments": "实验容器", "temporary": "临时/构建容器", "other": "其他"}


def _chart(series: dict, devices: dict, key: int, title: str, events: list, now: float) -> str:
    width, height, pad = 960, 200, 36
    start = now - 86400
    points = [v[key] for dev in series for v in series[dev]]
    top = max(points + [1]) * 1.1
    colors = ["#2a78d6", "#d84a4a", "#1baf7a"]
    lines, legend = [], []
    for i, dev in enumerate(sorted(series)):
        pts = " ".join(f"{pad + (b[0] - start) / 86400 * (width - pad - 8):.1f},"
                       f"{height - 20 - b[key] / top * (height - 40):.1f}" for b in series[dev])
        color = colors[i % len(colors)]
        lines.append(f'<polyline fill="none" stroke="{color}" stroke-width="1.6" points="{pts}"/>')
        legend.append(f'<span><i style="background:{color}"></i>{html.escape(devices.get(dev, {}).get("label", dev))} ({dev})</span>')
    marks = []
    for ts, project, kind, name in events:
        if kind != "start":
            continue
        x = pad + (ts - start) / 86400 * (width - pad - 8)
        color = PROJECT_COLORS.get(project, "#b0b0b0")
        marks.append(f'<line x1="{x:.1f}" x2="{x:.1f}" y1="{height - 20}" y2="{height - 12}" stroke="{color}" stroke-width="2">'
                     f'<title>{time.strftime("%H:%M", time.localtime(ts))} {html.escape(PROJECT_LABELS.get(project, project))} {html.escape(name)}</title></line>')
    ticks = []
    for h in range(0, 25, 3):
        x = pad + h / 24 * (width - pad - 8)
        ticks.append(f'<text x="{x:.0f}" y="{height - 1}" class="tick">{time.strftime("%H:%M", time.localtime(start + h * 3600))}</text>')
    grid = []
    for frac in (0, 0.5, 1):
        y = height - 20 - frac * (height - 40)
        grid.append(f'<line x1="{pad}" x2="{width - 8}" y1="{y:.0f}" y2="{y:.0f}" class="grid"/>'
                    f'<text x="2" y="{y + 4:.0f}" class="tick">{top / 1.1 * frac:.0f}</text>')
    unit = "KB/s" if key in (1, 3) else "次/s"
    return (f'<div class="card"><div class="card-h">{html.escape(title)}（{unit}，5 分钟平均；底部刻线 = 容器启动，颜色按项目）</div>'
            f'<svg viewBox="0 0 {width} {height}" class="chart">{"".join(grid)}{"".join(lines)}{"".join(marks)}{"".join(ticks)}</svg>'
            f'<div class="legend">{"".join(legend)}</div></div>')


def render_dashboard(st: dict) -> str:
    now = st["generated_at"]
    badge = {"ok": "ok", "warn": "warn", "crit": "crit", "unknown": "unk"}
    worst = max((LEVELS[f["level"]] for f in st["findings"]), default=0)
    counts = collections.Counter(f["level"] for f in st["findings"])
    tiles = []
    med = st.get("write_1h_median")
    tiles.append(("整机写入 · 1 小时中位数",
                  f"数据 {st.get('write_minutes', 0)} 分钟" if med is None else f"{med:.0f} KB/s",
                  "满 30 分钟后显示；问题现场 10-10 约 6300 KB/s"))
    lm = st.get("write_last_minute")
    tiles.append(("整机写入 · 最近 1 分钟", "—" if lm is None else f"{lm:.0f} KB/s", "含发布、实验造成的尖峰"))
    for dev, vol in sorted(st["volumes"].items()):
        pct = vol.get("used_pct")
        tiles.append((f"{vol.get('label', dev)} 已用", "—" if pct is None else f"{pct:.0f}%",
                      f"{_gb(vol.get('used'))} / {_gb(vol.get('total'))}"))
    img = st.get("images")
    if img:
        tiles.append(("可回收的镜像空间", _gb(img["reclaimable_bytes"]),
                      f"{img['image_count']} 个镜像，悬空 {img['dangling_count']} 个"))
    tile_html = "".join(f'<div class="tile"><div class="t-l">{html.escape(a)}</div><div class="t-v">{html.escape(b)}</div>'
                        f'<div class="t-s">{html.escape(c)}</div></div>' for a, b, c in tiles)
    groups = collections.OrderedDict([("global", [])] + [(p["id"], []) for p in st["projects"]])
    for f in st["findings"]:
        groups.setdefault(f["project"], []).append(f)
    names = {p["id"]: p["name"] for p in st["projects"]}
    sections = []
    for pid, items in groups.items():
        if not items:
            continue
        items = sorted(items, key=lambda f: -LEVELS[f["level"]])
        rows = "".join(f'<tr><td><span class="b {badge[f["level"]]}">{f["level"]}</span></td><td>{html.escape(f["label"])}</td>'
                       f'<td class="num">{html.escape(str(f["value"]))}</td><td class="muted">{html.escape(f["budget"])}</td></tr>'
                       for f in items)
        title = PROJECT_LABELS.get(pid) or names.get(pid, pid)
        sections.append(f'<div class="card"><div class="card-h">{html.escape(title)}</div>'
                        f'<table><tr><th>状态</th><th>检查项</th><th>当前</th><th>预算</th></tr>{rows}</table></div>')
    ev_rows = "".join(f'<tr><td>{time.strftime("%m-%d %H:%M", time.localtime(ts))}</td>'
                      f'<td><i class="dot" style="background:{PROJECT_COLORS.get(p, "#b0b0b0")}"></i>{html.escape(PROJECT_LABELS.get(p) or names.get(p, p))}</td>'
                      f'<td>{html.escape(k)}</td><td>{html.escape(n)}</td></tr>'
                      for ts, p, k, n in st["events"][-60:][::-1])
    errors = "".join(f"<div>{html.escape(k)}：{html.escape(v)}</div>" for k, v in st["errors"].items())
    overall = ["全部正常", "有需要关注的项", "有超出预算的项"][worst]
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta http-equiv="refresh" content="60">
<title>NAS 存储与写盘</title><style>
:root{{--bg:#f6f7f9;--card:#fff;--fg:#1d2330;--muted:#6b7385;--line:#e3e6ec;--ok:#1baf7a;--warn:#d88c1b;--crit:#d84a4a;--unk:#9aa1ad}}
@media (prefers-color-scheme:dark){{:root{{--bg:#12151b;--card:#1b2029;--fg:#e6e9ef;--muted:#98a0ae;--line:#2c3340}}}}
body{{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif}}
.wrap{{max-width:1100px;margin:0 auto;padding:20px 16px}}h1{{font-size:20px;margin:0 0 4px}}.sub{{color:var(--muted);margin-bottom:16px}}
.tiles{{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:10px;margin-bottom:14px}}
.tile,.card{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px}}
.t-l{{color:var(--muted);font-size:12px}}.t-v{{font-size:22px;font-weight:600;margin:2px 0}}.t-s{{color:var(--muted);font-size:12px}}
.card{{margin-bottom:12px;overflow-x:auto}}.card-h{{font-weight:600;margin-bottom:8px}}
table{{width:100%;border-collapse:collapse}}th,td{{text-align:left;padding:5px 8px;border-top:1px solid var(--line);vertical-align:top}}
th{{color:var(--muted);font-weight:500;border-top:none}}.num{{white-space:nowrap}}.muted{{color:var(--muted)}}
.b{{display:inline-block;min-width:44px;text-align:center;border-radius:4px;color:#fff;font-size:12px;padding:0 6px}}
.b.ok{{background:var(--ok)}}.b.warn{{background:var(--warn)}}.b.crit{{background:var(--crit)}}.b.unk{{background:var(--unk)}}
.chart{{width:100%;height:auto}}.chart .grid{{stroke:var(--line)}}.chart .tick{{fill:var(--muted);font-size:10px}}
.legend span{{margin-right:14px;color:var(--muted);font-size:12px}}.legend i,.dot{{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px;vertical-align:-1px}}
.err{{color:var(--crit);font-size:12px}}
</style></head><body><div class="wrap">
<h1>NAS 存储与写盘</h1>
<div class="sub">总体：{overall}（crit {counts.get("crit", 0)} · warn {counts.get("warn", 0)} · ok {counts.get("ok", 0)} · 未知 {counts.get("unknown", 0)}）· 更新于 {time.strftime("%F %H:%M:%S", time.localtime(now))} · 预算见 report-portal 仓库 monitor/projects.json</div>
<div class="tiles">{tile_html}</div>
{_chart(st["series"], st["devices"], 1, "写入速度（最近 24 小时）", st["events"], now)}
{_chart(st["series"], st["devices"], 3, "读取速度（最近 24 小时）", st["events"], now)}
{"".join(sections)}
<div class="card"><div class="card-h">容器启动记录（最近 24 小时，发布和实验会在这里出现）</div>
<table><tr><th>时间</th><th>归属</th><th>动作</th><th>容器</th></tr>{ev_rows or '<tr><td colspan="4" class="muted">暂无</td></tr>'}</table></div>
<div class="err">{errors}</div>
<div class="sub">看不到按进程/按容器的写盘量（需要特权，按约定不开）；各项目的贡献用存储增长、配置检查和上面的启动记录来判断。</div>
</div></body></html>"""


def build_app(monitor: Monitor) -> Starlette:
    async def dashboard(request: Request) -> Response:
        return HTMLResponse(render_dashboard(monitor.status(time.time())))

    async def api_status(request: Request) -> Response:
        st = monitor.status(time.time())
        st["series"] = {k: v for k, v in st["series"].items()}
        return JSONResponse(st)

    async def manifest(request: Request) -> Response:
        return JSONResponse({"source": "nas-monitor", "reports": [{
            "name": "NAS 存储与写盘",
            "desc": "整机读写速度、各项目存储预算、镜像与发布残留、容器写盘配置；越线发飞书",
            "url": DASHBOARD_PATH, "icon": "💾", "ready": True}]})

    async def health(request: Request) -> Response:
        code, body = monitor.health(time.time())
        return JSONResponse(body, status_code=code)

    return Starlette(routes=[
        Route(DASHBOARD_PATH, dashboard), Route("/", dashboard),
        Route("/api/nas/status", api_status), Route("/portal_manifest", manifest),
        Route("/health", health)])


def main():
    import uvicorn
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    # httpx 每个请求一行 INFO：每 5 分钟几十行，在这台 NAS 上就是实打实的写盘（T-0245）。
    logging.getLogger("httpx").setLevel(logging.WARNING)
    registry = json.loads(REGISTRY_PATH.read_text("utf-8"))
    monitor = Monitor(registry, Store(DB_PATH),
                      DockerReader(DOCKER_SOCKET) if os.path.exists(DOCKER_SOCKET) else None,
                      feishu_sender(FEISHU_FILE))
    stop = threading.Event()
    threading.Thread(target=monitor.run_forever, args=(stop,), daemon=True, name="collector").start()
    uvicorn.run(build_app(monitor), host=os.environ.get("NAS_MONITOR_BIND_HOST", "0.0.0.0"),
                port=int(os.environ.get("NAS_MONITOR_BIND_PORT", "8080")), log_level="warning")


if __name__ == "__main__":
    main()
