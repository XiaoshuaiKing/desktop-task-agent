"""状态投影的回归测试：从 tasks 表映射出"上一个任务怎么了"。"""
import unittest
import uuid
from pathlib import Path

from core.session_manager import init_db
from core.task_manager import TaskManager
from core.task_state_projection import (
    INTERRUPT_WINDOW_HOURS,
    build_state_projection,
    find_interrupted_task,
    render_state,
    summarize_steps,
)


class TaskStateProjectionTest(unittest.TestCase):
    def setUp(self):
        workspace = Path(__file__).resolve().parents[2]
        self.db = workspace / ("_tmp_state_%s.db" % uuid.uuid4().hex)
        init_db(self.db)
        self.manager = TaskManager(self.db)
        self.task_id = self.manager.create_task(user_id="u1", description="把报告发给张三")
        self.manager.update_task_status(self.task_id, "RUNNING")

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            leftover = Path(str(self.db) + suffix)
            if leftover.exists():
                try:
                    leftover.unlink()
                except OSError:
                    pass

    def _step(self, name, status):
        step_id = self.manager.add_tool_call(self.task_id, name, {})
        self.manager.update_step(self.task_id, step_id, status, result="ok")

    def test_01_no_interrupted_task_returns_empty(self):
        self.assertEqual(build_state_projection("u1", db_path=self.db), "")

    def test_02_marked_interrupted_is_projected(self):
        self._step("read_text_file", "SUCCEEDED")
        self.manager.update_task_status(self.task_id, "INTERRUPTED",
                                        stop_reason="process_restarted")
        text = build_state_projection("u1", db_path=self.db)

        self.assertIn("上一个任务未完成", text)
        self.assertIn("把报告发给张三", text)

    def test_03_counts_finished_and_unsettled(self):
        self._step("read_text_file", "SUCCEEDED")
        self._step("write_text_file", "SUCCEEDED")
        self._step("send_file_to_qq", "EXECUTING")
        self.manager.update_task_status(self.task_id, "INTERRUPTED")

        task = self.manager.get_task(self.task_id)
        counts = summarize_steps(task)
        self.assertEqual(counts["SUCCEEDED"], 2)
        self.assertEqual(counts["EXECUTING"], 1)

        text = render_state(task)
        self.assertIn("已完成 2 步", text)
        self.assertIn("结果未知 1 步", text)

    def test_04_unsettled_steps_carry_a_warning(self):
        self._step("send_file_to_qq", "EXECUTING")
        self.manager.update_task_status(self.task_id, "INTERRUPTED")
        text = render_state(self.manager.get_task(self.task_id))
        self.assertIn("可能已经生效", text)
        self.assertNotIn("可以安全重试", text)

    def test_05_no_unsettled_means_safe_to_retry(self):
        self._step("read_text_file", "SUCCEEDED")
        self.manager.update_task_status(self.task_id, "INTERRUPTED")
        text = render_state(self.manager.get_task(self.task_id))
        self.assertIn("可以安全重试", text)

    def test_06_running_task_is_not_projected(self):
        """只有 INTERRUPTED 才算；正在跑的不能误报。"""
        self.assertEqual(build_state_projection("u1", db_path=self.db), "")

    def test_07_other_users_task_not_projected(self):
        self.manager.update_task_status(self.task_id, "INTERRUPTED")
        self.assertEqual(build_state_projection("u2", db_path=self.db), "")

    def test_08_stale_interrupt_outside_window_is_ignored(self):
        self.manager.update_task_status(self.task_id, "INTERRUPTED")
        stale = find_interrupted_task("u1", db_path=self.db, window_hours=0)
        self.assertIsNone(stale)

    def test_09_render_none_is_empty(self):
        self.assertEqual(render_state(None), "")

    def test_10_manager_error_does_not_raise(self):
        class Boom:
            def get_unfinished_tasks(self, _):
                raise RuntimeError("db down")

        self.assertEqual(build_state_projection("u1", task_manager=Boom()), "")

    def test_11_window_default_is_one_day(self):
        self.assertEqual(INTERRUPT_WINDOW_HOURS, 24)


if __name__ == "__main__":
    unittest.main()
