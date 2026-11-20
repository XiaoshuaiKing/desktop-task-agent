"""长期记忆的回归测试：3 个正向 + 2 个反向。

反向用例是重点：记忆系统最容易坏的地方不是「想不起来」，而是「想起不该想的」。
"""
import unittest
import uuid
from pathlib import Path

from core.session_manager import init_db
from core.task_manager import TaskManager
from core.memory import (
    build_memory_block,
    extract_pinned,
    load_pinned,
    recall_history,
    render_block,
    save_pinned,
    wants_history,
)


class MemoryTestCase(unittest.TestCase):
    def setUp(self):
        workspace = Path(__file__).resolve().parents[2]
        self.db = workspace / ("_tmp_memory_%s.db" % uuid.uuid4().hex)
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

    # ---------- 正向 ----------

    def test_01_explicit_rule_is_stored(self):
        block = build_memory_block("u1", "以后生成的文件都放桌面", db_path=self.db)
        facts = load_pinned("u1", db_path=self.db)
        self.assertIn("output_dir", facts)
        self.assertTrue(facts["output_dir"].endswith("Desktop"))
        self.assertIn("生成的文件默认目录", block)

    def test_02_browser_rule_is_stored(self):
        build_memory_block("u1", "以后都用 Chrome 打开网页", db_path=self.db)
        self.assertEqual(load_pinned("u1", db_path=self.db)["browser"], "chrome")

    def test_03_history_recall_returns_similar_task(self):
        self.manager.create_task(user_id="u1", description="帮我生成一份周报")
        self.manager.create_task(user_id="u1", description="查一下今天天气")
        hits = recall_history("u1", "再生成一份周报", db_path=self.db)
        self.assertTrue(hits)
        self.assertIn("周报", hits[0]["description"])

    # ---------- 反向 ----------

    def test_04_changing_mind_overwrites_not_appends(self):
        build_memory_block("u1", "以后文件都放桌面", db_path=self.db)
        build_memory_block("u1", "以后文件都放 D:\\work", db_path=self.db)
        facts = load_pinned("u1", db_path=self.db)
        self.assertEqual(len(facts), 1, "改口后只应保留一条事实")
        self.assertEqual(facts["output_dir"], "D:\\work")

    def test_05_one_off_instruction_is_not_memorised(self):
        # 没有规则词（以后/默认/一直/都…），属于本次任务参数，不能进长期记忆
        build_memory_block("u1", "这次把文件放桌面", db_path=self.db)
        self.assertEqual(load_pinned("u1", db_path=self.db), {})

    def test_06_unrelated_task_does_not_recall(self):
        self.manager.create_task(user_id="u1", description="帮我生成一份周报")
        hits = recall_history("u1", "今天天气怎么样", db_path=self.db)
        self.assertEqual(hits, [])

    # ---------- 边界 ----------

    def test_07_render_is_empty_without_content(self):
        self.assertEqual(render_block({}, []), "")

    def test_08_extract_does_not_mutate_input(self):
        current = {"browser": "edge"}
        extract_pinned("以后都用 Chrome", current)
        self.assertEqual(current, {"browser": "edge"})

    def test_09_load_on_missing_row_is_empty(self):
        self.assertEqual(load_pinned("nobody", db_path=self.db), {})

    def test_10_save_then_load_roundtrip(self):
        save_pinned("u9", {"output_dir": "D:\\out", "browser": "edge"}, db_path=self.db)
        self.assertEqual(load_pinned("u9", db_path=self.db),
                         {"output_dir": "D:\\out", "browser": "edge"})

    # ---------- 按需召回 ----------

    def test_11_no_trigger_means_no_history_in_block(self):
        self.manager.create_task(user_id="u1", description="帮我生成一份周报")
        # 没提"上次/之前/一样"，不该召回
        block = build_memory_block("u1", "帮我生成一份周报", db_path=self.db)
        self.assertNotIn("历史任务", block)

    def test_12_trigger_pulls_in_history(self):
        self.manager.create_task(user_id="u1", description="帮我生成一份周报")
        block = build_memory_block("u1", "跟上次一样，再生成一份周报", db_path=self.db)
        self.assertIn("历史任务", block)
        self.assertIn("周报", block)

    def test_13_wants_history_detects_references(self):
        for text in ("上次那个怎么做的", "和之前一样", "再做一遍", "继续"):
            self.assertTrue(wants_history(text), text)
        for text in ("查一下天气", "打开浏览器", ""):
            self.assertFalse(wants_history(text), text)


if __name__ == "__main__":
    unittest.main()
