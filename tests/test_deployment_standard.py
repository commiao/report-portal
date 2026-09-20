"""report-portal 的部署准则机器检查。

准则文档会被绕过，因为它不参与任何判断；带机器检查的准则才有效。这个文件把
deploy-standard 里适用于本服务的几条，变成会当场变红的断言。

每个用例的 docstring 写它防的是哪一条、以及那条是怎么被踩出来的。
"""
import pathlib
import tempfile
import unittest
from datetime import datetime

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _executable(script: str) -> str:
    """只留会执行的行。

    变异验证时抓到的真实缺陷：release.sh 的注释里也写着 `merge-base --is-ancestor`
    和 `git archive`，于是把真正的闸门整行删掉、只留注释，`assertIn` 照样是绿的
    ——正是准则 17 说的「删掉被测代码仍然通过」。断言必须只看代码。
    """
    return "\n".join(l for l in script.splitlines() if not l.lstrip().startswith("#"))


RELEASE = _executable((ROOT / "deploy" / "release.sh").read_text(encoding="utf-8"))
COMPOSE = _executable((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
REDEPLOY = _executable((ROOT / "deploy" / "redeploy.sh").read_text(encoding="utf-8"))
DRIFT = _executable((ROOT / "deploy" / "check_source_drift.py").read_text(encoding="utf-8"))


class ReleasePathTests(unittest.TestCase):
    def test_only_publishes_commits_on_the_trunk(self):
        """准则 18：从分支直发，线上那份只存在于那条分支上，下一个从 main 出发的
        人会把它悄悄抹掉且没有冲突提示。用 is-ancestor 而不是「等于 origin/main」，
        因为回滚到更早的 commit 是正当操作。"""
        self.assertIn("merge-base --is-ancestor", RELEASE)
        self.assertIn("origin/main", RELEASE)
        # 闸必须真的拦住：不通过就得退出，而不是打印一行继续发。
        gate = next(l for l in RELEASE.splitlines() if "merge-base --is-ancestor" in l)
        idx = RELEASE.splitlines().index(gate)
        self.assertIn("die", "\n".join(RELEASE.splitlines()[idx:idx + 2]))

    def test_artifact_is_a_commit_not_the_working_tree(self):
        """准则 1：发布物必须等于某个 commit。`cat 工作树文件 | ssh` 会把本机任何
        未提交的改动一起发上去，于是「线上是哪个 commit」没有答案。"""
        # 真正的投放这一步（--format=tar 那条，区别于 dry-run 里只用来数条目的）
        publish = [l for l in RELEASE.splitlines() if "archive --format=tar" in l]
        self.assertEqual(len(publish), 1, "投放必须且只能走 git archive")
        self.assertNotIn("cat \"$REPO/$f\"", RELEASE)

    def test_backs_up_the_whole_tree_before_overwriting(self):
        """准则 5：备份范围 ⊇ 覆盖范围。git archive 覆盖整棵树，所以只备份「构建
        输入」那几个文件的话，第一次真跑就会把生产独有文件盖掉——kg-hub 踩过。"""
        self.assertIn("tar czf", RELEASE)
        self.assertIn("-C '$SRC' .", RELEASE)

    def test_verifies_what_actually_landed(self):
        """准则 26：不是「我传上去了」，是可机器验证的等于——落地后逐文件比哈希。"""
        self.assertIn("ls-tree -r --name-only", RELEASE)
        self.assertIn("sha256sum", RELEASE)
        self.assertIn("落地后与", RELEASE)

    def test_has_a_rollback_path(self):
        """准则 6/7：带着没有退路的发布切生产，就是 kg-hub「切了之后回不来」那种
        形态。本服务无状态，回滚 = 标签指回去再 up。"""
        self.assertIn("rollback", RELEASE)
        self.assertIn("自动回滚", RELEASE)
        self.assertIn("--no-build", RELEASE)


class ImmutableImageTests(unittest.TestCase):
    def test_compose_refuses_to_run_an_unpinned_image(self):
        """准则 1/26：给默认值会让没人指定版本时悄悄跑成 latest，线上是哪个 commit
        就再也说不清。用 `:?` 让 compose 当场失败。"""
        self.assertIn("${PORTAL_IMAGE_TAG:?", COMPOSE)
        self.assertNotIn("image: report-portal:latest", COMPOSE)


class BypassTests(unittest.TestCase):
    def test_the_old_working_tree_path_is_closed(self):
        """准则 21：绕过发布路径改一次生产，正式发布路径本身也会失效。只要旁路还
        在，闸门/备份/复核/回滚就全可以被绕过，那它们等于不存在。"""
        self.assertIn("exit 2", REDEPLOY)
        self.assertIn("已停用", REDEPLOY)
        self.assertNotIn("compose -p", REDEPLOY)


class DriftStatusContractTests(unittest.TestCase):
    """状态文件是 fleet-ops SessionStart 巡检读的契约，这里按它的解析方式实跑。

    上面几条是静态断言（文本里有没有那一句）；这几条是真的调函数、真的解析，
    因为格式写错不会有人发现——巡检只会显示成一条橙灯「状态读不懂」。
    """

    @staticmethod
    def _module():
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "portal_drift", ROOT / "deploy" / "check_source_drift.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def _parse(self, path):
        """完全照 ops-hook-context.sh 的读法：三段 tab 分隔 + ISO 时间。"""
        at, verdict, detail = pathlib.Path(path).read_text("utf-8").strip().split("\t", 2)
        datetime.fromisoformat(at.replace("Z", "+00:00"))  # 解析不了就当场抛
        return verdict, detail

    def test_status_file_matches_what_the_sweep_parses(self):
        mod = self._module()
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "sub" / "source-drift.status"   # 父目录不存在也要能写
            mod.write_status(str(p), "ok", "16 个文件等于 origin/main")
            verdict, detail = self._parse(p)
        self.assertEqual(verdict, "ok")
        self.assertEqual(detail, "16 个文件等于 origin/main")

    def test_a_failed_check_is_not_written_as_ok(self):
        """准则：「查不了」和「没漂」是两回事。写成 ok 的话，ssh 挂掉的那几天
        SessionStart 会一直播报体检通过。"""
        mod = self._module()
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "s"
            mod.write_status(str(p), "error", "取数失败：CalledProcessError")
            verdict, _ = self._parse(p)
        self.assertNotEqual(verdict, "ok")
        self.assertNotEqual(verdict, "drift")   # 也不能混进「漂了」那一类


class DriftDetectionTests(unittest.TestCase):
    def test_lists_both_sides_and_takes_the_union(self):
        """准则 4：只按 git 清单去查，永远看不见「只在生产上存在」的文件——
        kg-hub 的 hot_config_reconciliation.py 360 行在生产跑着、git 里没有，
        而当时的检测显示「✅ 对得上」。"""
        self.assertIn("ls-tree", DRIFT)
        self.assertIn("find . -type f", DRIFT)
        self.assertIn("set(got) - set(want)", DRIFT)

    def test_a_failed_lookup_is_not_reported_as_clean(self):
        """取数失败必须和「一致」区分开，否则 ssh 挂掉会被读成体检通过。"""
        self.assertIn("取数失败，不作判决", DRIFT)
        self.assertIn("return 2", DRIFT)


if __name__ == "__main__":
    unittest.main()
