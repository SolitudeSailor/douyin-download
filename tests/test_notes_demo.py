"""视频笔记 Demo 的静态结构与关键交互回归（离线）。"""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "docs" / "design" / "workbuddy-notes-demo.html"

__test__ = False


class NotesDemoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = DEMO.read_text(encoding="utf-8")

    def test_quick_experience_is_the_primary_entry(self):
        self.assertIn('id="quickStart"', self.html)
        self.assertIn('id="tryDemo"', self.html)
        self.assertIn('id="formalSetup"', self.html)
        self.assertLess(self.html.index('id="quickStart"'), self.html.index('id="setupCard"'))
        self.assertIn('id="setupCard" aria-labelledby="setupTitle" hidden', self.html)

    def test_product_language_is_consistent(self):
        self.assertIn("视频笔记助手", self.html)
        self.assertIn("WORKBUDDY VIDEO NOTES", self.html)
        self.assertNotIn("DOUYIN VIDEO DOWNLOADER", self.html)
        self.assertNotIn("抖音视频下载器</h1>", self.html)

    def test_demo_and_formal_modes_are_explicit(self):
        self.assertIn("体验模式使用示例数据，不读取视频、不连接 WorkBuddy。", self.html)
        self.assertIn("正式接入设置", self.html)
        self.assertIn("state.demoMode=true", self.html)
        self.assertIn("state.demoMode=false", self.html)

    def test_advanced_prompt_is_collapsed(self):
        details = re.search(r'<details class="import-help task-details">(.*?)</details>',
                            self.html, re.DOTALL)
        self.assertIsNotNone(details)
        self.assertIn('id="taskPrompt"', details.group(1))
        self.assertIn("查看或编辑高级任务指令", details.group(1))

    def test_all_literal_dom_lookups_have_matching_ids(self):
        ids = set(re.findall(r'id="([A-Za-z][A-Za-z0-9_-]*)"', self.html))
        lookups = set(re.findall(r"\$\('([A-Za-z][A-Za-z0-9_-]*)'\)", self.html))
        self.assertEqual(lookups - ids, set())


if __name__ == "__main__":
    unittest.main(verbosity=2)
