"""nas_monitor 的行为测试：采集解析、存储统计、检查打分、告警状态机、health、页面。

监控的价值在于「越线时会红、坏了会报」，所以每类检查都先造一个越线的输入看它红不红。
"""
import json
import os
import re
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from starlette.testclient import TestClient

import nas_monitor as nm

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = json.loads((ROOT / "monitor" / "projects.json").read_text("utf-8"))
GiB = nm.GiB

DISKSTATS = """\
   9       2 md2 100 0 2000 0 500 0 8000 0 0 0 0
   9       3 md3 10 0 200 0 50 0 800 0 0 0 0
   8       0 sda 1 0 1 0 1 0 1 0 0 0 0
"""


def find(findings, key):
    return next(f for f in findings if f["key"] == key)


def registry(**global_overrides):
    reg = json.loads(json.dumps(REGISTRY))
    reg["global"].update(global_overrides)
    return reg


class CollectTests(unittest.TestCase):
    def test_diskstats_keeps_only_registered_devices(self):
        counts = nm.parse_diskstats(DISKSTATS, ["md2", "md3"])
        self.assertEqual(counts, {"md2": (100, 2000, 500, 8000), "md3": (10, 200, 50, 800)})

    def test_minute_rates_are_per_second_and_skip_counter_resets(self):
        prev = {"md2": (100, 2000, 500, 8000), "md3": (10, 200, 50, 800)}
        cur = {"md2": (160, 2120, 620, 20000), "md3": (5, 100, 40, 700)}
        rates = nm.minute_rates(prev, cur, 60)
        self.assertAlmostEqual(rates["md2"]["wkb"], (20000 - 8000) * 512 / 1024 / 60)
        self.assertAlmostEqual(rates["md2"]["wops"], 2.0)
        self.assertAlmostEqual(rates["md2"]["rkb"], 120 * 512 / 1024 / 60)
        self.assertNotIn("md3", rates)          # 计数倒退（重启/回绕）不算

    def test_disk_usage_counts_real_blocks_and_does_not_follow_links(self):
        with tempfile.TemporaryDirectory() as d:
            Path(d, "a").write_bytes(b"x" * 100_000)
            Path(d, "sub").mkdir()
            Path(d, "sub", "b").write_bytes(b"y" * 50_000)
            outside = tempfile.NamedTemporaryFile(delete=False)
            outside.write(b"z" * 5_000_000)
            outside.close()
            self.addCleanup(os.unlink, outside.name)
            os.symlink(outside.name, os.path.join(d, "link"))
            big_dir = tempfile.TemporaryDirectory()
            self.addCleanup(big_dir.cleanup)
            Path(big_dir.name, "huge").write_bytes(b"w" * 3_000_000)
            os.symlink(big_dir.name, os.path.join(d, "dirlink"))
            size, files = nm.disk_usage(d)
            self.assertEqual(files, 4)                 # 两个符号链接各算一个条目，不进入目标
            self.assertGreaterEqual(size, 150_000)
            self.assertLess(size, 1_000_000)   # 没有把 5MB 的链接目标算进来
            self.assertIsNone(nm.disk_usage(os.path.join(d, "missing")))
            single, n = nm.disk_usage(os.path.join(d, "a"))
            self.assertEqual(n, 1)
            self.assertGreaterEqual(single, 100_000)
            self.assertLess(single, 200_000)           # 按实际块数，不是放大的名义大小

    def test_children_counts_matching_entries_only(self):
        with tempfile.TemporaryDirectory() as d:
            for name in ("20261013T042535Z-9825a5d87542", "20261014T055323Z-0db2e5a89a84", ".build-abc", "notes"):
                Path(d, name).mkdir()
                Path(d, name, "f").write_bytes(b"x" * 4096)
            old = time.time() - 5 * 3600
            os.utime(Path(d, ".build-abc"), (old, old))
            spec = {"dir": d, "pattern": r"^20\d{6}T\d{6}Z-"}
            c = nm.scan_children(spec, time.time())
            self.assertEqual(c["count"], 2)
            self.assertEqual(c["names"], ["20261013T042535Z-9825a5d87542", "20261014T055323Z-0db2e5a89a84"])
            b = nm.scan_children({"dir": d, "pattern": r"^\.build-"}, time.time())
            self.assertEqual(b["count"], 1)
            self.assertGreater(b["oldest_age_h"], 4.9)
            self.assertIsNone(nm.scan_children({"dir": os.path.join(d, "x"), "pattern": "."}, time.time()))

    def test_freshness_reads_the_json_timestamp_not_the_file_mtime(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "status.json")
            p.write_text(json.dumps({"updated_at": "2026-10-13T15:05:00+00:00"}))
            now = 1_791_910_000.0          # 2026-10-14 ~01:26Z
            age = nm.file_age_hours({"path": str(p), "json_time_field": "updated_at"}, now)
            self.assertAlmostEqual(age, (now - 1_791_903_900) / 3600, places=3)
            self.assertIsNone(nm.file_age_hours({"path": str(p) + "x", "json_time_field": "updated_at"}, now))

    def test_policy_fields_keep_only_log_driver_and_health_interval(self):
        inspect = {"Config": {"Env": ["FALKORDB_PASSWORD=never-keep-this"],
                              "Healthcheck": {"Test": ["CMD", "true"], "Interval": 10_000_000_000}},
                   "HostConfig": {"LogConfig": {"Type": "db", "Config": {"x": "y"}}}}
        fields = nm.policy_fields(inspect)
        self.assertEqual(fields, {"log_driver": "db", "healthcheck_interval_s": 10.0})
        self.assertNotIn("never-keep-this", json.dumps(fields))
        self.assertIsNone(nm.policy_fields({"Config": {}})["healthcheck_interval_s"])
        default = nm.policy_fields({"Config": {"Healthcheck": {"Test": ["CMD", "true"]}}})
        self.assertEqual(default["healthcheck_interval_s"], 30.0)   # docker 默认 30s

    def test_image_summary_counts_dangling_and_tags_per_repo(self):
        df = {"Images": [
            {"RepoTags": ["kg-hub-server:a"], "Size": 500, "SharedSize": 400, "Containers": 1},
            {"RepoTags": ["kg-hub-server:b", "kg-hub-server:latest"], "Size": 600, "SharedSize": 400, "Containers": 0},
            {"RepoTags": ["<none>:<none>"], "Size": 300, "SharedSize": 100, "Containers": 0},
            {"RepoTags": None, "Size": 50, "SharedSize": -1, "Containers": 0}],
            "BuildCache": [{"Size": 70}, {"Size": 30}], "LayersSize": 1234}
        s = nm.image_summary(df, REGISTRY)
        self.assertEqual(s["dangling_count"], 2)
        self.assertEqual(s["dangling_bytes"], 200 + 50)
        self.assertEqual(s["build_cache_bytes"], 100)
        self.assertEqual(s["repo_tags"]["kg-hub-server"], 3)
        self.assertEqual(s["repo_tags"]["recruitment-intel-service"], 0)
        # docker CLI 算法：全部层 1234 减去在用镜像的独占部分（500-400）
        self.assertEqual(s["reclaimable_bytes"], 1234 - 100)
        self.assertEqual(s["images_bytes"], 1234)

    def test_sampling_keeps_going_while_a_slow_docker_call_blocks(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        disk = Path(tmp.name, "diskstats")
        disk.write_text(DISKSTATS)
        release = __import__("threading").Event()

        class SlowDocker(FakeDocker):
            def containers(self):
                release.wait(5)              # 模拟 /system/df 那样的慢调用
                return []
        mon = nm.Monitor(REGISTRY, nm.Store(Path(tmp.name, "m.sqlite3")), SlowDocker(), None,
                         diskstats_path=str(disk))
        stop = __import__("threading").Event()
        with mock.patch.object(nm, "SAMPLE_SECONDS", 0.05), mock.patch.object(nm, "MINUTE", 1), \
             mock.patch.object(nm, "SCAN_SECONDS", 10 ** 9):
            mon.last["scan"] = time.time()
            t = __import__("threading").Thread(target=mon.run_forever, args=(stop,), daemon=True)
            t.start()
            time.sleep(2.5)
            sampled = mon.last["sample"]
            stop.set()
            release.set()
            t.join(5)
        self.assertGreater(sampled, 0)        # docker 还卡着，采样已经在记账了

    def test_system_df_gets_a_longer_timeout_than_other_docker_calls(self):
        reader = nm.DockerReader.__new__(nm.DockerReader)
        reader._client = mock.Mock()
        reader._client.get.return_value.json.return_value = {"Images": []}
        reader.system_df()
        self.assertEqual(reader._client.get.call_args.kwargs["timeout"], nm.DF_TIMEOUT_SECONDS)
        self.assertGreaterEqual(nm.DF_TIMEOUT_SECONDS, 300)     # 实测 >120s

    def test_containers_are_attributed_to_projects(self):
        self.assertEqual(nm.project_of(REGISTRY, "kg-hub-server", {"com.docker.compose.project": "kg-hub"}), "kg-hub")
        self.assertEqual(nm.project_of(REGISTRY, "model-gateway", {}), "credvault")
        self.assertEqual(nm.project_of(REGISTRY, "recruitment-postgres",
                                       {"com.docker.compose.project": "recruitment-intel-nas"}), "zhaopin")
        self.assertEqual(nm.project_of(REGISTRY, "t0195-v39-s14-joint", {}), "experiments")
        self.assertEqual(nm.project_of(REGISTRY, "charming_chebyshev", {}), "temporary")
        self.assertEqual(nm.project_of(REGISTRY, "report-portal", {"com.docker.compose.project": "report-portal-src"}), "other")


class EvaluateTests(unittest.TestCase):
    def snap(self, **kw):
        base = {"write_minutes_1h": [800.0] * 60,
                "volumes": {"md2": {"label": "volume1", "used_pct": 50, "used": 1, "total": 2},
                            "md3": {"label": "volume2", "used_pct": 73, "used": 1, "total": 2}},
                "images": {"dangling_count": 3, "dangling_bytes": GiB // 2, "build_cache_bytes": GiB // 2,
                           "repo_tags": {"kg-hub-server": 3, "credvault-model-gateway": 2,
                                         "recruitment-intel-service": 2, "recruitment-intel-backup": 2,
                                         "recruitment-intel-browser-sidecar": 2},
                           "image_count": 10, "reclaimable_bytes": GiB},
                "exited_unowned": 2, "paths": {}, "growth": {}, "children": {}, "freshness": {},
                "policies": {"kg-hub": {"kg-hub-falkordb": {"log_driver": "local", "healthcheck_interval_s": 60.0}},
                             "credvault": {}, "zhaopin": {}}}
        base.update(kw)
        return base

    def test_write_rate_uses_the_hourly_median_and_needs_30_minutes(self):
        spikes = [800.0] * 50 + [20000.0] * 10          # 10 分钟发布尖峰不该触发
        self.assertEqual(find(nm.evaluate(REGISTRY, self.snap(write_minutes_1h=spikes)), "global:write-rate")["level"], "ok")
        high = [6300.0] * 60                             # 问题现场的水平
        self.assertEqual(find(nm.evaluate(REGISTRY, self.snap(write_minutes_1h=high)), "global:write-rate")["level"], "crit")
        self.assertEqual(find(nm.evaluate(REGISTRY, self.snap(write_minutes_1h=[9e9] * 10)), "global:write-rate")["level"], "unknown")

    def test_volume_dangling_and_exited_thresholds(self):
        f = nm.evaluate(REGISTRY, self.snap(
            volumes={"md3": {"label": "volume2", "used_pct": 91, "used": 1, "total": 2}},
            images=dict(self.snap()["images"], dangling_bytes=14 * GiB, dangling_count=745),
            exited_unowned=68))
        self.assertEqual(find(f, "global:volume-md3")["level"], "crit")
        self.assertEqual(find(f, "global:dangling-images")["level"], "crit")
        self.assertIn("745 个，独占至少", find(f, "global:dangling-images")["value"])
        self.assertEqual(find(f, "global:exited-containers")["level"], "crit")

    def test_path_budget_growth_and_missing_path(self):
        f = nm.evaluate(REGISTRY, self.snap(paths={("kg-hub", "ledger"): int(19.2 * GiB)},
                                            growth={("kg-hub", "ledger"): 1300.0}))
        self.assertEqual(find(f, "kg-hub:path-ledger")["level"], "crit")       # 2026-10-10 的 19.2GB
        self.assertEqual(find(f, "kg-hub:growth-ledger")["level"], "ok")
        self.assertEqual(find(f, "kg-hub:path-falkordb")["level"], "unknown")  # 没扫到不冒充 ok
        f = nm.evaluate(REGISTRY, self.snap(growth={("kg-hub", "ledger"): 3200.0}))
        self.assertEqual(find(f, "kg-hub:growth-ledger")["level"], "crit")

    def test_release_staging_and_build_leftovers(self):
        children = {("zhaopin", "release-staging"): {"count": 8, "bytes": int(23 * GiB), "oldest_age_h": 40, "names": []},
                    ("zhaopin", "build-leftovers"): {"count": 1, "bytes": 11 * 1024 ** 2, "oldest_age_h": 20, "names": []}}
        f = nm.evaluate(REGISTRY, self.snap(children=children))
        self.assertEqual(find(f, "zhaopin:children-release-staging")["level"], "crit")
        self.assertEqual(find(f, "zhaopin:children-build-leftovers")["level"], "warn")
        ok = {("zhaopin", "release-staging"): {"count": 3, "bytes": int(8.7 * GiB), "oldest_age_h": 40, "names": []},
              ("zhaopin", "build-leftovers"): {"count": 0, "bytes": 0, "oldest_age_h": None, "names": []}}
        f = nm.evaluate(REGISTRY, self.snap(children=ok))
        self.assertEqual(find(f, "zhaopin:children-release-staging")["level"], "ok")
        self.assertEqual(find(f, "zhaopin:children-build-leftovers")["level"], "ok")

    def test_one_entry_over_the_keep_count_is_already_a_warning(self):
        children = {("zhaopin", "release-staging"): {"count": 4, "bytes": int(8 * GiB), "oldest_age_h": 40, "names": []}}
        self.assertEqual(find(nm.evaluate(REGISTRY, self.snap(children=children)),
                              "zhaopin:children-release-staging")["level"], "warn")

    def test_entries_older_than_max_age_warn_even_within_the_keep_count(self):
        reg = json.loads(json.dumps(REGISTRY))
        spec = next(c for p in reg["projects"] for c in p["children"] if c["id"] == "build-leftovers")
        spec["keep"] = 2
        young = {("zhaopin", "build-leftovers"): {"count": 1, "bytes": 1024, "oldest_age_h": 1, "names": []}}
        old = {("zhaopin", "build-leftovers"): {"count": 1, "bytes": 1024, "oldest_age_h": 5, "names": []}}
        self.assertEqual(find(nm.evaluate(reg, self.snap(children=young)), "zhaopin:children-build-leftovers")["level"], "ok")
        self.assertEqual(find(nm.evaluate(reg, self.snap(children=old)), "zhaopin:children-build-leftovers")["level"], "warn")

    def test_one_off_recovery_snapshots_must_not_linger(self):
        children = {("credvault", "recovery-snapshots"): {"count": 3, "bytes": int(0.65 * GiB), "oldest_age_h": 160, "names": []}}
        self.assertEqual(find(nm.evaluate(REGISTRY, self.snap(children=children)),
                              "credvault:children-recovery-snapshots")["level"], "crit")

    def test_freshness_flags_a_missed_nightly_prune(self):
        f = nm.evaluate(REGISTRY, self.snap(freshness={("kg-hub", "nightly-prune"): 30.0}))
        self.assertEqual(find(f, "kg-hub:fresh-nightly-prune")["level"], "warn")
        f = nm.evaluate(REGISTRY, self.snap(freshness={("kg-hub", "nightly-prune"): None}))
        self.assertEqual(find(f, "kg-hub:fresh-nightly-prune")["level"], "warn")   # 读不到也要提醒

    def test_image_tags_over_budget(self):
        tags = dict(self.snap()["images"]["repo_tags"], **{"kg-hub-server": 138})
        f = nm.evaluate(REGISTRY, self.snap(images=dict(self.snap()["images"], repo_tags=tags)))
        self.assertEqual(find(f, "kg-hub:image-tags-kg-hub-server")["level"], "crit")
        self.assertEqual(find(f, "credvault:image-tags-credvault-model-gateway")["level"], "ok")

    def test_container_policy_flags_frequent_health_checks_and_db_logs(self):
        policies = {"zhaopin": {"recruitment-postgres": {"log_driver": "db", "healthcheck_interval_s": 10.0},
                                "recruitment-intel-api": {"log_driver": "db", "healthcheck_interval_s": None}},
                    "kg-hub": {}, "credvault": {}}
        f = find(nm.evaluate(REGISTRY, self.snap(policies=policies)), "zhaopin:container-policy")
        self.assertEqual(f["level"], "warn")
        self.assertIn("recruitment-postgres 健康检查每 10s", f["value"])
        self.assertIn("recruitment-intel-api 日志驱动 db", f["value"])
        ok = find(nm.evaluate(REGISTRY, self.snap()), "kg-hub:container-policy")
        self.assertEqual(ok["level"], "ok")


class AlertTests(unittest.TestCase):
    F = {"project": "zhaopin", "key": "zhaopin:x", "label": "发布包", "value": "8 个", "budget": "≤ 3"}

    def step(self, level, state, now):
        return nm.alert_transitions([dict(self.F, level=level)], state, now)

    def test_escalation_sends_once_then_stays_quiet(self):
        lines, st = self.step("warn", {}, 0)
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("🟡"))
        lines, st = self.step("warn", st, 300)
        self.assertEqual(lines, [])
        lines, st = self.step("crit", st, 600)
        self.assertTrue(lines[0].startswith("🔴"))

    def test_crit_repeats_every_12_hours_and_recovery_is_announced(self):
        _, st = self.step("crit", {}, 0)
        lines, st = self.step("crit", st, 11 * 3600)
        self.assertEqual(lines, [])
        lines, st = self.step("crit", st, 12 * 3600 + 1)
        self.assertEqual(len(lines), 1)
        lines, st = self.step("ok", st, 13 * 3600)
        self.assertTrue(lines[0].startswith("✅ 已恢复"))
        lines, st = self.step("ok", st, 14 * 3600)
        self.assertEqual(lines, [])

    def test_unknown_neither_recovers_nor_escalates(self):
        _, st = self.step("warn", {}, 0)
        lines, st2 = self.step("unknown", st, 300)
        self.assertEqual(lines, [])
        self.assertEqual(st2["zhaopin:x"]["level"], "warn")
        lines, _ = self.step("warn", st2, 600)
        self.assertEqual(lines, [])

    def test_feishu_payload_is_signed_when_a_secret_is_configured(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"url": "https://example.invalid/hook", "secret": "s3"}, f)
        self.addCleanup(os.unlink, f.name)
        send = nm.feishu_sender(f.name)
        resp = mock.Mock(status_code=200)
        resp.json.return_value = {"code": 0}
        with mock.patch.object(nm.httpx, "post", return_value=resp) as post:
            self.assertTrue(send("hello"))
        body = post.call_args.kwargs["json"]
        self.assertEqual(body["content"]["text"], "hello")
        self.assertIn("sign", body)
        self.assertIsNone(nm.feishu_sender(""))
        self.assertIsNone(nm.feishu_sender(f.name + ".missing"))


class FakeDocker:
    def __init__(self):
        self.calls = []

    def containers(self):
        return [{"Id": "1", "Names": ["/recruitment-postgres"], "State": "running",
                 "Labels": {"com.docker.compose.project": "recruitment-intel-nas"}},
                {"Id": "2", "Names": ["/t0195-v1"], "State": "exited", "Labels": {}},
                {"Id": "4", "Names": ["/recruitment-intel-migrate"], "State": "exited",
                 "Labels": {"com.docker.compose.project": "recruitment-intel-nas"}},
                {"Id": "3", "Names": ["/lucid_spence"], "State": "exited", "Labels": {}}]

    def container_policy(self, cid):
        self.calls.append(cid)
        return {"log_driver": "local", "healthcheck_interval_s": 60.0}

    def events(self, since, until):
        return [{"time": until - 5, "Action": "start", "Actor": {"Attributes": {"name": "t0195-v1"}}}]

    def system_df(self):
        return {"Images": [], "BuildCache": []}


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.disk = Path(self.tmp.name, "diskstats")
        self.disk.write_text(DISKSTATS)
        self.store = nm.Store(Path(self.tmp.name, "m.sqlite3"))
        self.sent = []
        self.mon = nm.Monitor(REGISTRY, self.store, FakeDocker(), lambda t: self.sent.append(t) or True,
                              diskstats_path=str(self.disk))

    def test_minutes_are_flushed_in_one_batch_and_reloaded_after_restart(self):
        now = time.time()
        for i in range(3):
            self.mon.record_minute(int(now) - 180 + i * 60, {"md2": {"wkb": 100.0, "wops": 5, "rkb": 1, "rops": 1},
                                                            "md3": {"wkb": 50.0, "wops": 2, "rkb": 1, "rops": 1}})
        self.assertEqual(self.store.query("SELECT COUNT(*) FROM io")[0][0], 0)   # 还没落盘
        self.mon.flush(now)
        self.assertEqual(self.store.query("SELECT COUNT(*) FROM io")[0][0], 6)
        again = nm.Monitor(REGISTRY, self.store, None, None, diskstats_path=str(self.disk))
        self.assertEqual(again.write_minutes(3600), [150.0, 150.0, 150.0])

    def test_docker_refresh_attributes_events_and_counts_unowned_exits(self):
        self.mon.refresh_docker(time.time(), with_df=True)
        self.assertEqual(self.mon.snapshot["exited_unowned"], 2)
        self.assertIn("recruitment-postgres", self.mon.snapshot["policies"]["zhaopin"])
        self.assertEqual(self.mon.pending_events[0][1], "experiments")
        self.assertEqual(self.mon.snapshot["images"]["image_count"], 0)

    def test_evaluate_sends_one_feishu_message_for_new_problems(self):
        self.mon.snapshot.update(volumes={"md3": {"label": "volume2", "used_pct": 95, "used": 1, "total": 2}})
        self.mon.evaluate_and_alert(time.time())
        self.assertEqual(len(self.sent), 1)
        self.assertIn("volume2 已用空间", self.sent[0])
        self.mon.evaluate_and_alert(time.time())
        self.assertEqual(len(self.sent), 1)
        state = nm.Store(Path(self.tmp.name, "m.sqlite3")).load_alert_state()
        self.assertEqual(state["global:volume-md3"]["level"], "crit")     # 重启后不会重复发

    def test_problems_found_before_feishu_is_configured_are_sent_once_it_is(self):
        self.store.save_alert_state({})
        quiet = nm.Monitor(REGISTRY, self.store, None, None, diskstats_path=str(self.disk))
        quiet.snapshot.update(volumes={"md3": {"label": "volume2", "used_pct": 95, "used": 1, "total": 2}})
        quiet.evaluate_and_alert(time.time())
        self.assertEqual(self.store.load_alert_state(), {})          # 没发出去就不算通知过
        sent = []
        later = nm.Monitor(REGISTRY, self.store, None, lambda t: sent.append(t) or True, diskstats_path=str(self.disk))
        later.snapshot.update(volumes={"md3": {"label": "volume2", "used_pct": 95, "used": 1, "total": 2}})
        later.evaluate_and_alert(time.time())
        self.assertEqual(len(sent), 1)

    def test_a_failed_send_is_retried_next_round(self):
        attempts = []
        flaky = nm.Monitor(REGISTRY, self.store, None, lambda t: attempts.append(t) and False,
                           diskstats_path=str(self.disk))
        flaky.snapshot.update(volumes={"md3": {"label": "volume2", "used_pct": 95, "used": 1, "total": 2}})
        flaky.evaluate_and_alert(time.time())
        flaky.evaluate_and_alert(time.time())
        self.assertEqual(len(attempts), 2)
        self.assertIn("feishu", flaky.errors)

    def test_health_goes_red_when_sampling_stops_but_not_for_findings(self):
        now = time.time()
        self.mon.started = now - 3600
        self.mon.last.update(sample=now - 30, scan=now - 60, docker=now - 60)
        self.mon.findings = [{"level": "crit"}]
        code, body = self.mon.health(now)
        self.assertEqual((code, body["status"], body["worst"]), (200, "ok", "crit"))
        self.mon.last["sample"] = now - 600
        code, body = self.mon.health(now)
        self.assertEqual(code, 503)
        self.assertIn("磁盘采样", body["stale"])
        self.mon.last.update(sample=now - 30, scan=now - 3 * nm.SCAN_SECONDS)
        self.assertEqual(self.mon.health(now)[0], 503)

    def test_health_tolerates_startup_but_not_a_dead_sampler(self):
        now = time.time()
        self.mon.started = now - 30
        self.assertEqual(self.mon.health(now)[1]["status"], "warming")
        self.mon.started = now - 120
        self.assertEqual(self.mon.health(now)[0], 503)       # 2 分钟还没第一次采样


class AppTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        disk = Path(tmp.name, "diskstats")
        disk.write_text(DISKSTATS)
        self.mon = nm.Monitor(REGISTRY, nm.Store(Path(tmp.name, "m.sqlite3")), None, None, diskstats_path=str(disk))
        now = time.time()
        for i in range(40):
            self.mon.record_minute(int(now) - 2400 + i * 60, {"md2": {"wkb": 700.0, "wops": 30, "rkb": 9, "rops": 1},
                                                              "md3": {"wkb": 100.0, "wops": 5, "rkb": 2, "rops": 1}})
        self.mon.snapshot.update(volumes={"md2": {"label": "volume1", "used_pct": 50, "used": GiB, "total": 2 * GiB}})
        self.mon.evaluate_and_alert(now)
        self.client = TestClient(nm.build_app(self.mon))

    def test_manifest_follows_the_portal_contract(self):
        body = self.client.get("/portal_manifest").json()
        self.assertEqual(body["source"], "nas-monitor")
        card = body["reports"][0]
        self.assertEqual(card["url"], "/dashboard/nas")
        self.assertTrue(card["ready"])

    def test_dashboard_renders_numbers_charts_and_findings(self):
        r = self.client.get("/dashboard/nas")
        self.assertEqual(r.status_code, 200)
        self.assertIn("800 KB/s", r.text)            # 1 小时中位数 = 700 + 100
        self.assertIn("<polyline", r.text)
        self.assertIn("整机写入（1 小时中位数，md2+md3）", r.text)
        self.assertIn("任务账本 model-attempts.sqlite3", r.text)

    def test_hourly_median_is_withheld_until_30_minutes_of_data(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        mon = nm.Monitor(REGISTRY, nm.Store(Path(tmp.name, "m.sqlite3")), None, None)
        now = time.time()
        for i in range(5):
            mon.record_minute(int(now) - 300 + i * 60, {"md2": {"wkb": 3000.0, "wops": 1, "rkb": 1, "rops": 1}})
        st = mon.status(now)
        self.assertIsNone(st["write_1h_median"])
        self.assertIn("数据 5 分钟", nm.render_dashboard(st))

    def test_status_api_is_json(self):
        body = self.client.get("/api/nas/status").json()
        self.assertAlmostEqual(body["write_1h_median"], 800.0)
        self.assertTrue(body["series"]["md2"])


class RegistryTests(unittest.TestCase):
    def test_every_registered_path_is_under_a_compose_mount(self):
        compose = (ROOT / "docker-compose.yml").read_text("utf-8")
        block = compose.split("\n  nas_monitor:\n", 1)[1].split("\nnetworks:\n", 1)[0]
        mounts = re.findall(r"- [^:\s]+:(/host/[^:\s]+):ro", block)
        self.assertTrue(mounts)
        paths = [s["statvfs_path"] for s in REGISTRY["devices"].values()]
        for p in REGISTRY["projects"]:
            paths += [s["path"] for s in p.get("paths", [])]
            paths += [s["dir"] for s in p.get("children", [])]
            paths += [s["path"] for s in p.get("freshness", [])]
        for path in paths:
            self.assertTrue(any(path == m or path.startswith(m + "/") for m in mounts), path)

    def test_patterns_compile_and_budgets_are_ordered(self):
        for p in REGISTRY["projects"]:
            for spec in p.get("children", []):
                re.compile(spec["pattern"])
            for spec in p.get("paths", []):
                self.assertLess(spec["warn_gb"], spec["crit_gb"], spec["id"])
        for key, b in REGISTRY["global"].items():
            self.assertLess(b["warn"], b["crit"], key)


if __name__ == "__main__":
    unittest.main()


class ReleaseScriptTests(unittest.TestCase):
    RELEASE = (ROOT / "deploy" / "release.sh").read_text("utf-8")

    def starter(self):
        return self.RELEASE.split("start_services() {", 1)[1].split("\n}\n", 1)[0]

    def test_monitor_is_started_with_the_portal_when_the_image_has_it(self):
        s = self.starter()
        self.assertIn("-f /app/nas_monitor.py", s)
        self.assertIn("up -d --no-build $SERVICE $MONITOR_SERVICE", s)
        # 绑定挂载的源目录先建好，再起容器
        self.assertLess(s.index("mkdir -p $MONITOR_DIRS"), s.index("$SERVICE $MONITOR_SERVICE"))

    def test_rolling_back_to_an_image_without_the_monitor_stops_it(self):
        s = self.starter()
        older = s.split("else", 1)[1]
        self.assertIn("up -d --no-build $SERVICE >", older)
        self.assertIn("rm -f $MONITOR_CONTAINER", older)

    def test_acceptance_requires_a_healthy_monitor_on_the_new_tag(self):
        accept = self.RELEASE.split("monitor_ok=1", 1)[1].split("\nfi\n", 1)[0]
        self.assertIn('check_monitor "$SHORT" || monitor_ok=0', accept)
        self.assertIn('[ "$monitor_ok" != 1 ]', accept)
        self.assertIn('start_services "$PREV"', accept)
        check = self.RELEASE.split("check_monitor() {", 1)[1].split("\n}\n", 1)[0]
        self.assertIn('[ "$mimg" = "$IMAGE:$tag" ] && [ "$mcode" = "200" ]', check)

    def test_monitor_dirs_match_the_compose_mounts(self):
        compose = (ROOT / "docker-compose.yml").read_text("utf-8")
        dirs = re.search(r'MONITOR_DIRS="\$\{PORTAL_MONITOR_DIRS:-([^}]*)\}"', self.RELEASE).group(1).split()
        for d in dirs:
            self.assertIn(f"- {d}:", compose)
