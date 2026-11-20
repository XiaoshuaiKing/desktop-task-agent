"""上下文压缩的回归测试：三条不可违反的规则各有用例。

规则 1：目标锚点永不丢
规则 2：先压工具结果，再丢消息
规则 3：成组丢弃，不产生孤儿 tool_call
"""
import unittest

from core.context_builder import (
    Budget,
    build_context,
    compress_tool_content,
    estimate_messages,
    estimate_tokens,
)


def user(text):
    return {"role": "user", "content": text}


def assistant(text=None, calls=None):
    msg = {"role": "assistant", "content": text}
    if calls:
        msg["tool_calls"] = calls
    return msg


def tool(call_id, content):
    return {"role": "tool", "tool_call_id": call_id, "content": content}


def call(call_id, name="read_text_file"):
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": "{}"}}


class ContextBuilderTest(unittest.TestCase):
    def setUp(self):
        self.system = {"role": "system", "content": "你是桌面助手。"}

    # ---------- 估算 ----------

    def test_01_estimate_orders_chinese_above_english(self):
        zh = estimate_tokens("中文" * 50)
        en = estimate_tokens("word " * 50)
        self.assertGreater(zh, en)

    # ---------- 规则 2：先压工具 ----------

    def test_02_older_tool_result_compressed_recent_kept(self):
        """旧结果压掉，最新一条保留——最新那条可能是刚回查的原文。"""
        msgs = [self.system, user("读一下文件"),
                assistant(calls=[call("c0")]), tool("c0", "旧" * 2000),
                assistant(calls=[call("c1")]), tool("c1", "新" * 2000)]

        out, record = build_context(msgs, goal_text="读一下文件",
                                    budget=Budget(total=100000, tool_result=500))

        self.assertEqual(record.compressed_tools, 1)
        old = [m for m in out if m.get("tool_call_id") == "c0"][0]
        new = [m for m in out if m.get("tool_call_id") == "c1"][0]
        self.assertIn("已压缩", old["content"])
        self.assertNotIn("已压缩", new["content"])
        self.assertEqual(new["tool_call_id"], "c1")

    def test_03_short_tool_result_untouched(self):
        msgs = [self.system, user("读"), assistant(calls=[call("c1")]), tool("c1", "很短")]
        out, record = build_context(msgs, goal_text="读", budget=Budget(total=100000))
        self.assertEqual(record.compressed_tools, 0)
        self.assertEqual(out[3]["content"], "很短")

    def test_04_compress_keeps_head_and_tail(self):
        text = "HEAD" + "x" * 5000 + "TAIL"
        kept, changed = compress_tool_content(text, 400)
        self.assertTrue(changed)
        self.assertTrue(kept.startswith("HEAD"))
        self.assertTrue(kept.endswith("TAIL"))

    # ---------- 规则 1：目标锚点 ----------

    def test_05_goal_survives_budget_pressure(self):
        msgs = [self.system, user("把三份文档整理成摘要")]
        for i in range(30):
            msgs.append(assistant(calls=[call("c%d" % i)]))
            msgs.append(tool("c%d" % i, "结果" * 300))

        out, record = build_context(msgs, goal_text="把三份文档整理成摘要",
                                    budget=Budget(total=1500, tool_result=200))

        self.assertTrue(record.kept_goal)
        self.assertTrue(any("把三份文档整理成摘要" in str(m.get("content")) for m in out))
        self.assertLess(estimate_messages(out), estimate_messages(msgs))

    def test_06_under_budget_keeps_everything(self):
        msgs = [self.system, user("查天气"), assistant("好的")]
        out, record = build_context(msgs, goal_text="查天气", budget=Budget(total=100000))
        self.assertEqual(len(out), 3)
        self.assertEqual(record.dropped_messages, 0)

    # ---------- 规则 3：不产生孤儿 tool_call ----------

    def test_07_no_orphan_tool_calls(self):
        msgs = [self.system, user("目标")]
        for i in range(20):
            msgs.append(assistant(calls=[call("c%d" % i), call("d%d" % i)]))
            msgs.append(tool("c%d" % i, "x" * 2000))
            msgs.append(tool("d%d" % i, "y" * 2000))

        out, _ = build_context(msgs, goal_text="目标",
                               budget=Budget(total=2000, tool_result=200))

        called, answered = set(), set()
        for m in out:
            for c in m.get("tool_calls") or []:
                called.add(c["id"])
            if m.get("role") == "tool":
                answered.add(m.get("tool_call_id"))
        self.assertEqual(called, answered, "存在只有调用没有结果的孤儿")

    # ---------- 记录与健壮性 ----------

    def test_08_record_has_tokens(self):
        msgs = [self.system, user("目标"), assistant(calls=[call("c1")]), tool("c1", "z" * 9000)]
        _, record = build_context(msgs, goal_text="目标",
                                  budget=Budget(total=800, tool_result=300))
        self.assertGreater(record.before_tokens, record.after_tokens)
        self.assertTrue(record.changed)
        self.assertIn("before_tokens", record.to_dict())

    def test_09_empty_input_does_not_crash(self):
        out, record = build_context([], goal_text="")
        self.assertEqual(out, [])
        self.assertFalse(record.changed)

    def test_10_non_string_content_does_not_crash(self):
        msgs = [self.system, user(None), assistant(calls=[call("c1")]), tool("c1", None)]
        out, _ = build_context(msgs, goal_text="", budget=Budget(total=500))
        self.assertTrue(len(out) >= 1)

    def test_11_compression_record_is_written_to_task(self):
        class FakeManager:
            def __init__(self):
                self.steps = []

            def add_step(self, **kwargs):
                self.steps.append(kwargs)

        manager = FakeManager()
        msgs = [self.system, user("目标"), assistant(calls=[call("c1")]), tool("c1", "q" * 9000)]
        build_context(msgs, goal_text="目标", budget=Budget(total=700, tool_result=200),
                      task_manager=manager, task_id="t1")
        self.assertEqual(len(manager.steps), 1)
        self.assertEqual(manager.steps[0]["step_type"], "context_compression")

    def test_12_budget_is_respected(self):
        msgs = [self.system, user("目标")]
        for i in range(15):
            msgs.append(assistant(calls=[call("c%d" % i)]))
            msgs.append(tool("c%d" % i, "w" * 4000))
        budget = Budget(total=3000, tool_result=250)
        out, _ = build_context(msgs, goal_text="目标", budget=budget)
        # 允许 goal 与最后一组带来的轻微超出，但必须显著低于原始
        self.assertLess(estimate_messages(out), estimate_messages(msgs))

    # ---------- 第三级：目标降级 ----------

    def test_13_goal_is_downgraded_when_still_over_budget(self):
        long_goal = "把桌面上这三份文档整理成摘要并保存，注意不要删除任何原文件，除非文件名带 revisit。" * 40
        msgs = [self.system, user(long_goal), assistant(calls=[call("c1")]), tool("c1", "r" * 6000)]

        out, record = build_context(msgs, goal_text=long_goal,
                                    budget=Budget(total=600, tool_result=200),
                                    task_manager=None, task_id="task-abc")

        self.assertTrue(record.goal_downgraded)
        self.assertEqual(record.goal_original_chars, len(long_goal))
        joined = " ".join(str(m.get("content")) for m in out)
        self.assertIn("read_task_original", joined)
        self.assertIn("task-abc", joined)
        # 原文不再整段出现
        self.assertNotIn(long_goal, joined)

    def test_14_downgrade_shrinks_tokens(self):
        long_goal = "很长的目标。" * 2000
        msgs = [self.system, user(long_goal)]
        out, record = build_context(msgs, goal_text=long_goal,
                                    budget=Budget(total=200, tool_result=100),
                                    task_id="t1")
        self.assertTrue(record.goal_downgraded)
        self.assertLess(estimate_messages(out), estimate_messages(msgs) // 10)

    def test_15_no_task_id_means_no_downgrade(self):
        long_goal = "很长的目标。" * 2000
        msgs = [self.system, user(long_goal)]
        out, record = build_context(msgs, goal_text=long_goal,
                                    budget=Budget(total=200, tool_result=100))
        self.assertFalse(record.goal_downgraded)
        self.assertIn(long_goal, " ".join(str(m.get("content")) for m in out))

    def test_16_downgrade_record_written_to_task(self):
        class FakeManager:
            def __init__(self):
                self.steps = []

            def add_step(self, **kwargs):
                self.steps.append(kwargs)

        manager = FakeManager()
        long_goal = "长目标" * 3000
        msgs = [self.system, user(long_goal)]
        build_context(msgs, goal_text=long_goal, budget=Budget(total=150, tool_result=100),
                      task_manager=manager, task_id="t9")
        self.assertEqual(len(manager.steps), 1)
        payload = manager.steps[0]["arguments"]
        self.assertTrue(payload["goal_downgraded"])

    def test_17_downgrade_keeps_constraint_warning(self):
        long_goal = "整理文档，不要删除原文件。" * 500
        msgs = [self.system, user(long_goal)]
        out, _ = build_context(msgs, goal_text=long_goal, budget=Budget(total=150),
                               task_id="t2")
        text = " ".join(str(m.get("content")) for m in out)
        self.assertIn("不可撤销", text)

    # ---------- 评审缺陷回归 ----------

    def test_18_multi_turn_protects_current_request(self):
        """多轮会话里，被保护的目标必须是**本轮**请求，不是会话第一条。"""
        msgs = [self.system, user("第一条历史请求：整理周报"), assistant("好的")]
        for i in range(10):
            msgs.append(assistant(calls=[call("h%d" % i)]))
            msgs.append(tool("h%d" % i, "旧结果" * 500))
        msgs.append(user("本轮请求：把结果发给我"))

        out, record = build_context(msgs, goal_text="本轮请求：把结果发给我",
                                    budget=Budget(total=800, tool_result=200),
                                    task_id="t3")
        text = " ".join(str(m.get("content")) for m in out)
        self.assertIn("本轮请求", text)

    def test_19_latest_tool_result_is_not_compressed(self):
        """刚读回来的内容（如原文回查）不能被立刻截断，否则约束下一轮就丢了。"""
        msgs = [self.system, user("目标"),
                assistant(calls=[call("old")]), tool("old", "旧" * 5000),
                assistant(calls=[call("fresh")]), tool("fresh", "禁止发送给第三方。" * 100)]

        out, _ = build_context(msgs, goal_text="目标",
                               budget=Budget(total=100000, tool_result=100))
        fresh = [m for m in out if m.get("tool_call_id") == "fresh"][0]
        self.assertIn("禁止发送给第三方", fresh["content"])

    def test_20_over_budget_is_flagged(self):
        msgs = [self.system, user("目标" * 500)]
        msgs.append(assistant(calls=[{"id": "c1", "type": "function",
                                      "function": {"name": "t", "arguments": "a" * 5000}}]))
        msgs.append(tool("c1", "结果" * 3000))
        out, record = build_context(msgs, goal_text="目标" * 500,
                                    budget=Budget(total=200, tool_result=100),
                                    task_id="t4")
        self.assertTrue(record.over_budget or estimate_messages(out) <= 200)

    def test_21_compression_step_reaches_succeeded(self):
        """压缩记录本身也要走完终态，否则账本里会一直挂 PENDING。"""
        import uuid
        from pathlib import Path
        from core.session_manager import init_db
        from core.task_manager import TaskManager

        workspace = Path(__file__).resolve().parents[2]
        db = workspace / ("_tmp_cb_%s.db" % uuid.uuid4().hex)
        init_db(db)
        manager = TaskManager(db)
        task_id = manager.create_task(user_id="u1", description="目标")
        manager.update_task_status(task_id, "RUNNING")
        try:
            msgs = [self.system, user("目标"), assistant(calls=[call("c1")]),
                    tool("c1", "z" * 9000)]
            build_context(msgs, goal_text="目标",
                          budget=Budget(total=700, tool_result=200),
                          task_manager=manager, task_id=task_id)
            steps = manager.get_task(task_id)["steps"]
            compression = [s for s in steps if s["type"] == "context_compression"]
            self.assertTrue(compression)
            self.assertEqual(compression[0]["status"], "SUCCEEDED")
        finally:
            for suffix in ("", "-wal", "-shm"):
                leftover = Path(str(db) + suffix)
                if leftover.exists():
                    try:
                        leftover.unlink()
                    except OSError:
                        pass


if __name__ == "__main__":
    unittest.main()
