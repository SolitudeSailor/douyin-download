"""验证仓库协作规范包含提交和测试要求。"""

import unittest
from pathlib import Path


class AgentsDocumentTests(unittest.TestCase):
    def test_required_rules(self):
        document = Path(__file__).resolve().parents[1] / "AGENTS.md"
        text = document.read_text(encoding="utf-8")
        self.assertTrue(text.startswith("# 注意事项\n"))
        self.assertIn(
            "- 每次改动完成后，都必须创建一个对应的 Git commit，以便后续追踪和回滚。",
            text,
        )
        self.assertIn(
            "- 每次改动后，都必须编写或更新相关测试，并在交付给用户前，确保所有测试和验证全部通过。",
            text,
        )


if __name__ == "__main__":
    unittest.main()
