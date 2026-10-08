"""验证 GitHub 发布所需配置及脚本可移植性。"""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
__test__ = False


class GitHubReleaseTests(unittest.TestCase):
    def test_ci_runs_project_suite_on_windows(self):
        workflow = (ROOT / ".github/workflows/tests.yml").read_text(encoding="utf-8")
        self.assertIn("runs-on: windows-latest", workflow)
        self.assertIn('python-version: "3.13"', workflow)
        self.assertIn("python -X utf8 tests/run_all.py", workflow)

    def test_portable_scripts_do_not_embed_private_machine_paths(self):
        files = [
            ROOT / "secure/seal.bat",
            ROOT / "secure/unseal.bat",
            ROOT / "docs/probes/verify_esc.py",
        ]
        forbidden = ("C:\\Users\\" + "Solitude" + "Sailor", "E:\\AI\\workbuddy")
        for path in files:
            text = path.read_text(encoding="utf-8")
            for value in forbidden:
                self.assertNotIn(value, text, f"{path} 仍包含本机路径")

    def test_public_security_guidance_exists(self):
        security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
        self.assertIn("不要在公开 Issue 中提交 Cookie", security)
        self.assertIn("build/audit_release.py", security)


if __name__ == "__main__":
    unittest.main()
