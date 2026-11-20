"""会话裁剪的回归测试：主维度是 token 预算，轮数只做兜底。"""
import unittest

from core.context_builder import estimate_messages
from core.session_manager import (
    SESSION_MAX_TOKENS,
    SESSION_MAX_TURNS,
    trim_session,
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


def call(call_id):
    return {"id": call_id, "type": "function",
            "function": {"name": "read_text_file", "arguments": "{}"}}


class TrimSessionTest(unittest.TestCase):
    def setUp(self):
        self.system = {"role": "system", "content": "你是桌面助手。"}

    def test_01_short_history_untouched(self):
        msgs = [self.system, user("你好"), assistant("在")]
        before = len(msgs)
        out = trim_session(msgs)
        self.assertEqual(len(out), before)

    def test_02_system_prompt_always_kept(self):
        msgs = [self.system]
        for i in range(40):
            msgs.append(user("需求 %d" % i))
            msgs.append(assistant("好"))
        out = trim_session(msgs)
        self.assertEqual(out[0], self.system)

    def test_03_turn_cap_is_a_fallback(self):
        msgs = [self.system]
        for i in range(40):
            msgs.append(user("需求 %d" % i))
            msgs.append(assistant("好"))
        out = trim_session(msgs, max_tokens=10 ** 7)   # 把 token 维度放开，只看轮数
        kept_users = sum(1 for m in out if m.get("role") == "user")
        self.assertLessEqual(kept_users, SESSION_MAX_TURNS)

    def test_04_token_budget_drops_old_messages(self):
        msgs = [self.system]
        for i in range(20):
            msgs.append(user("第 %d 个需求" % i))
            msgs.append(assistant(calls=[call("c%d" % i)]))
            msgs.append(tool("c%d" % i, "结果" * 1500))   # 每条 ~3000 字
        before = estimate_messages(msgs)
        out = trim_session(msgs, max_tokens=4000)
        after = estimate_messages(out)
        self.assertLess(after, before)
        self.assertLessEqual(after, 4000 * 1.5)   # 允许保护项带来的少量超出

    def test_05_latest_request_survives(self):
        msgs = [self.system]
        for i in range(20):
            msgs.append(user("旧需求 %d" % i))
            msgs.append(assistant(calls=[call("c%d" % i)]))
            msgs.append(tool("c%d" % i, "旧结果" * 1200))
        msgs.append(user("这是最新请求"))
        out = trim_session(msgs, max_tokens=3000)
        self.assertIn("这是最新请求", " ".join(str(m.get("content")) for m in out))

    def test_06_no_orphan_tool_calls(self):
        msgs = [self.system]
        for i in range(15):
            msgs.append(user("需求 %d" % i))
            msgs.append(assistant(calls=[call("c%d" % i)]))
            msgs.append(tool("c%d" % i, "结果" * 2000))
        out = trim_session(msgs, max_tokens=2500)
        called, answered = set(), set()
        for m in out:
            for c in m.get("tool_calls") or []:
                called.add(c["id"])
            if m.get("role") == "tool":
                answered.add(m.get("tool_call_id"))
        self.assertEqual(called, answered)

    def test_07_empty_and_odd_input_do_not_crash(self):
        self.assertEqual(trim_session([]), [])
        msgs = [self.system, {"role": "user", "content": None}, assistant("x")]
        self.assertTrue(len(trim_session(msgs)) >= 1)

    def test_08_tool_results_are_compressed_before_dropping(self):
        msgs = [self.system, user("读文件"), assistant(calls=[call("c1")]),
                tool("c1", "内容" * 4000), user("再说一遍")]
        out = trim_session(msgs, max_tokens=3000)
        tool_msgs = [m for m in out if m.get("role") == "tool"]
        if tool_msgs:
            self.assertLess(len(tool_msgs[0]["content"]), 8000)


if __name__ == "__main__":
    unittest.main()
