"""中断检测的回归测试：只验证「发现并标记」，不涉及恢复执行。"""
import tempfile
import unittest
import uuid
from pathlib import Path

from core.session_manager import init_db
from core.task_manager import TaskManager
from core.recovery import find_stale_tasks, format_report, mark_stale_interrupted


class RecoveryTestCase(unittest.TestCase):
    def setUp(self):
        # 临时库用文件而非目录：当前环境对新建目录有限制，文件可直接写。
        workspace = Path(__file__).resolve().parents[2]
        self.db = workspace / ("_tmp_recovery_%s.db" % uuid.uuid4().hex)
        init_db(self.db)
        self.manager = TaskManager(self.db)

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            leftover = Path(str(self.db) + suffix)
            if leftover.exists():
                try:
                    leftover.unlink()
                except OSError:
                    pass

    def _task(self, status="RUNNING", user="u1"):
        task_id = self.manager.create_task(user_id=user, description="demo")
        if status != "PENDING":
            self.manager.update_task_status(task_id, status)
        return task_id

    def _status(self, task_id):
        return self.manager.get_task(task_id)["status"]

    def test_marks_running_and_pending_but_not_finished(self):
        running = self._task("RUNNING")
        pending = self._task("PENDING")
        done = self._task("RUNNING")
        self.manager.finish_task(done, "SUCCEEDED")

        report = mark_stale_interrupted(db_path=self.db)

        self.assertEqual(report["scanned"], 2)
        self.assertEqual(self._status(running), "INTERRUPTED")
        self.assertEqual(self._status(pending), "INTERRUPTED")
        self.assertEqual(self._status(done), "SUCCEEDED")

    def test_stop_reason_is_recorded(self):
        task_id = self._task("RUNNING")
        mark_stale_interrupted(db_path=self.db)
        self.assertEqual(
            self.manager.get_task(task_id)["stop_reason"], "process_restarted"
        )

    def test_dry_run_does_not_write(self):
        task_id = self._task("RUNNING")

        report = mark_stale_interrupted(db_path=self.db, dry_run=True)

        self.assertTrue(report["dry_run"])
        self.assertEqual(report["scanned"], 1)
        self.assertEqual(report["marked"], [task_id])
        self.assertEqual(self._status(task_id), "RUNNING")

    def test_second_run_is_noop(self):
        self._task("RUNNING")
        first = mark_stale_interrupted(db_path=self.db)
        second = mark_stale_interrupted(db_path=self.db)

        self.assertEqual(len(first["marked"]), 1)
        self.assertEqual(second["scanned"], 0)
        self.assertEqual(second["marked"], [])

    def test_counts_steps_left_executing(self):
        task_id = self._task("RUNNING")
        step_id = self.manager.add_tool_call(task_id, "read_text_file", {})
        self.manager.update_step(task_id, step_id, "EXECUTING")
        done_step = self.manager.add_tool_call(task_id, "open_software", {})
        self.manager.update_step(task_id, done_step, "SUCCEEDED")

        report = mark_stale_interrupted(db_path=self.db)

        self.assertEqual(report["steps_executing"], 1)

    def test_find_stale_tasks_is_read_only(self):
        task_id = self._task("RUNNING")
        stale = find_stale_tasks(db_path=self.db)

        self.assertEqual([t["task_id"] for t in stale], [task_id])
        self.assertEqual(self._status(task_id), "RUNNING")

    def test_waits_for_confirmation_is_stale_too(self):
        task_id = self._task("RUNNING")
        self.manager.update_task_status(task_id, "WAITING_CONFIRMATION")

        report = mark_stale_interrupted(db_path=self.db)

        self.assertEqual(report["scanned"], 1)
        self.assertEqual(self._status(task_id), "INTERRUPTED")

    def test_format_report_mentions_executing(self):
        task_id = self._task("RUNNING")
        step_id = self.manager.add_tool_call(task_id, "send_file_to_qq", {})
        self.manager.update_step(task_id, step_id, "EXECUTING")

        text = format_report(mark_stale_interrupted(db_path=self.db))

        self.assertIn("EXECUTING", text)
        self.assertIn("1", text)

    def test_empty_database_reports_nothing(self):
        report = mark_stale_interrupted(db_path=self.db)
        self.assertEqual(report["scanned"], 0)
        self.assertEqual(format_report(report), "中断检测：无遗留任务")


if __name__ == "__main__":
    unittest.main()
