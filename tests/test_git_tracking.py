"""验证项目基线保留工程文件，并忽略凭据和本地生成数据。"""

import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
__test__ = False


class GitTrackingTests(unittest.TestCase):
    def test_tracking_boundaries(self):
        ignored = {
            "config.yaml", "data/config.yaml", "data/browser_profile/Cookies",
            "data/Download/example.mp4", "data/logs/error.log",
            "secure/vault.7z", "secure/vault.manifest.json",
            "secure/vault.7z.tmp", ".workbuddy/memory/MEMORY.md",
            "dist/douyin-tool.exe", "dist_test/douyin-tool.exe",
            "release/setup.exe", "build_tmp/cache.bin",
            ".venv/Scripts/python.exe", "src/douyin_tool/__pycache__/core.pyc",
        }
        tracked = {
            "AGENTS.md", ".gitignore", "README.md", "SECURITY.md", "pyproject.toml",
            ".github/workflows/tests.yml",
            "requirements.txt", "src/douyin_tool/core.py",
            "src/douyin_tool/web/templates/index.html", "tests/run_all.py",
            "build/build.bat", "build/douyin_tool.spec", "docs/使用说明.md",
            "data/README.md", "secure/seal.py", "secure/README.md",
        }
        result = subprocess.run(
            ["git", "check-ignore", "--no-index", "--stdin", "-z"],
            input=("\0".join(sorted(ignored | tracked)) + "\0").encode("utf-8"),
            cwd=ROOT, capture_output=True, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(set(result.stdout.decode("utf-8").rstrip("\0").split("\0")), ignored)


if __name__ == "__main__":
    unittest.main()
