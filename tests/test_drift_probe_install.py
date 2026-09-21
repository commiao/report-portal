#!/usr/bin/env python3
"""漂移巡检安装脚本重载 launchd 作业的方式 —— 这块此前没有测试。

出处（fleet-ops T-0142，2026-09-22）：`bootout` 返回不等于作业已退干净，
紧跟着 `bootstrap` 会失败且不重试，结果是**旧的已卸、新的没装**。
批量切 4 个作业时四个全灭，服务停了约两分钟。

这个脚本原本**做对了一半**：用 Label 不用文件名（准则 27），注释里还写着理由。
漏的是另一半。**记住一条准则不等于记住相邻的那条** —— 所以处置不是再写一条
注释提醒，是把两半都收进 `launchd_reload`（它自己从 plist 读 Label，
也自己等旧作业退干净），并禁止旁路。
"""

from __future__ import annotations

import os
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "deploy/mac/install-drift-probe.sh"


class DriftProbeInstallTests(unittest.TestCase):

    def setUp(self):
        self.source = SCRIPT.read_text("utf-8")

    def test_不自己发装载命令(self):
        hits = re.findall(r"launchctl\s+(bootstrap|load)\b", self.source)
        self.assertFalse(hits, (
            f"脚本自己发了 {sorted(set(hits))} —— 手写的那两行少了"
            f"「等旧作业退干净」，改用 fleet-ops 的 launchd_reload（T-0142）。"))

    def test_用的是_fleet_ops_的公共实现(self):
        self.assertIn("launchd_reload", self.source)
        self.assertIn("platform/darwin/launchd.sh", self.source)

    def test_公共库不在时明确失败而不是回退(self):
        """钉的是**文案**不是退出码。

        守卫对「会不会失败」是冗余的（后面 `.` 一个不存在的文件，`set -e`
        自己就挂了）。它的价值是把 `No such file or directory` 换成一条
        能照着做的提示 —— 一条能照做的报错和一条只说文件不在的报错，
        排查成本差一个量级。
        """
        done = subprocess.run(["bash", str(SCRIPT)], capture_output=True, text=True,
                              env=dict(os.environ,
                                       FLEET_OPS_LAUNCHD_LIB="/nonexistent/launchd.sh"))
        self.assertNotEqual(0, done.returncode)
        self.assertIn("缺少 fleet-ops 的 launchd 库", done.stderr)
        self.assertNotIn("已装载", done.stdout, "库都没有却往下装了")


if __name__ == "__main__":
    unittest.main()
