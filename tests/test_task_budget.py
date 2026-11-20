"""预算提示的回归测试：只给 messages 给不了的东西。"""
import unittest

from core.task_budget import (
    STEP_WARN_RATIO,
    TIME_WARN_SECONDS,
    build_budget_block,
    render_budget,
)


class TaskBudgetTest(unittest.TestCase):
    def test_01_renders_budget_and_limit(self):
        text = render_budget(3, 25, 150)
        self.assertIn("剩余约 150 秒", text)
        self.assertIn("步数上限 25", text)

    def test_02_does_not_duplicate_what_messages_has(self):
        text = render_budget(3, 25, 150)
        self.assertNotIn("已完成", text)
        self.assertNotIn("最后动作", text)
        self.assertNotIn("第 3", text)

    def test_03_warns_when_time_is_short(self):
        self.assertIn("收尾", render_budget(2, 25, TIME_WARN_SECONDS - 5))

    def test_04_warns_when_steps_near_limit(self):
        self.assertIn("收尾", render_budget(int(25 * STEP_WARN_RATIO), 25, 200))

    def test_05_no_warning_in_comfortable_budget(self):
        self.assertNotIn("收尾", render_budget(3, 25, 200))

    def test_06_step_is_not_leaked_as_data(self):
        self.assertNotIn("7", render_budget(7, 25, 200))

    def test_07_no_max_steps_returns_empty(self):
        self.assertEqual(render_budget(3, None, 100), "")
        self.assertEqual(render_budget(3, 0, 100), "")

    def test_08_block_never_raises(self):
        self.assertEqual(build_budget_block(None, None, None), "")
        self.assertEqual(build_budget_block(1, None, 0), "")

    def test_09_negative_remaining_is_clamped(self):
        self.assertIn("剩余约 0 秒", render_budget(3, 25, -10))


if __name__ == "__main__":
    unittest.main()
