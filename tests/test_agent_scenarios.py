"""
QQ Agent 代表性任务场景回归测试。

测试范围：
1. 单工具任务
2. 多工具串联任务
3. 工具执行失败
4. 用户中途取消（当前工具执行结束后停止派发后续工具）
5. 多用户并发任务
6. 参数错误后由模型修正

重要说明：
- 本文件使用 Mock 模拟模型输出和工具执行，不调用真实 LLM、QQ API、浏览器或 Windows 桌面工具。
- TaskManager 使用临时 SQLite 数据库，不会写入项目的 data/agent.db。
- 多用户并发测试验证 run_agent 并行执行及任务记录隔离，不等同于完整 QQ 消息回调并发测试。
- 取消测试使用 threading.Event 模拟 Bot 发来的取消信号，验证当前工具结束后停止派发后续工具。
"""

import copy
import json
import sqlite3
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import core.agent as agent
from core.task_state import TaskState
from interfaces.command_handler import handle_cancel_command


class AgentScenarioTests(unittest.TestCase):
    """通过模拟模型和工具，验证 Agent 主循环的代表性执行场景。"""

    def setUp(self):
        # 每个测试使用独立临时目录和数据库，避免污染真实项目数据。
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "agent_test.db"
        self._create_test_database()

        # run_agent 从 core.agent.DB_PATH 创建 TaskManager。
        # 将它替换为临时数据库路径，测试结束后自动恢复。
        self.db_path_patch = patch.object(agent, "DB_PATH", self.db_path)
        self.db_path_patch.start()
        self.addCleanup(self.db_path_patch.stop)
        self.addCleanup(self.temp_dir.cleanup)

    def _create_test_database(self):
        """创建与当前项目 TaskManager 兼容的最小 tasks 表。"""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute(
                """
                CREATE TABLE tasks (
                    task_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    session_id TEXT,
                    description TEXT NOT NULL,
                    status TEXT NOT NULL,
                    steps TEXT NOT NULL DEFAULT '[]',
                    stop_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    reply_status TEXT NOT NULL DEFAULT 'NOT_SENT',
                    reply_error TEXT,
                    reply_sent_at TEXT
                )
                """
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _messages(user_text):
        """构造最小对话上下文。"""
        return [
            {"role": "system", "content": "你是一个测试用 Agent。"},
            {"role": "user", "content": user_text},
        ]

    @staticmethod
    def _tool_call(name, arguments, call_id):
        """构造 OpenAI 兼容格式的 Tool Call。"""
        return {
            "id": call_id,
            "type": "function",
            "function": {
                "name": name,
                "arguments": json.dumps(arguments, ensure_ascii=False),
            },
        }

    @staticmethod
    def _assistant_tool_message(*tool_calls):
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": list(tool_calls),
        }

    @staticmethod
    def _assistant_final_message(content):
        return {
            "role": "assistant",
            "content": content,
            "tool_calls": [],
        }

    @staticmethod
    def _no_skill(model_type, messages, call_model):
        """测试时不触发 Skill 路由，避免额外模型调用。"""
        return copy.deepcopy(messages)

    def _run_agent(self, user_id, user_text, max_steps=10, timeout=20, task_state=None):
        """统一调用当前项目的 run_agent，并要求返回 task_id。"""
        return agent.run_agent(
            model_type="api",
            messages=self._messages(user_text),
            session_id=user_id,
            role="user",
            max_steps=max_steps,
            timeout=timeout,
            return_task_id=True,
            task_state=task_state,
        )

    def _get_task(self, task_id):
        """从临时数据库读取任务和步骤。"""
        return agent.TaskManager(self.db_path).get_task(task_id)

    @staticmethod
    def _successful_fake_tool(message, role):
        """模拟一个成功工具，返回 use_tool 约定的六元组。"""
        tool_call = message["tool_calls"][0]
        function = tool_call["function"]
        arguments = json.loads(function["arguments"])
        result = {
            "status": "success",
            "message": f"{function['name']} 执行完成",
            "arguments": arguments,
        }
        return (
            tool_call["id"],
            result,
            message,
            function["name"],
            arguments,
            "success",
        )

    def test_01_single_tool_task(self):
        """单工具：模型请求一次工具，收到 Observation 后生成最终回复。"""
        model_outputs = [
            self._assistant_tool_message(
                self._tool_call("fake_get_info", {"topic": "天气"}, "call-1")
            ),
            self._assistant_final_message("已取得天气信息。"),
        ]

        with (
            patch.object(agent, "apply_skill", side_effect=self._no_skill),
            patch.object(agent, "call_model", side_effect=model_outputs),
            patch.object(agent, "use_tool", side_effect=self._successful_fake_tool),
        ):
            func_id, answer, trace, task_id = self._run_agent(
                "user-single", "查询天气"
            )

        task = self._get_task(task_id)
        self.assertIsNone(func_id)
        self.assertEqual(answer, "已取得天气信息。")
        self.assertEqual(len(trace), 1)
        self.assertEqual(trace[0]["action"], "fake_get_info")
        self.assertEqual(trace[0]["status"], "success")
        self.assertEqual(task["status"], "SUCCEEDED")
        self.assertEqual(task["steps"][0]["status"], "SUCCEEDED")
        self.assertEqual(task["steps"][-1]["type"], "final_response")

    def test_02_multi_tool_chained_task(self):
        """多工具串联：第二次模型调用必须能看到第一个工具的 Observation。"""
        model_observations = []
        executed_tools = []

        def fake_model(model_type, runtime_messages, remaining):
            tool_messages = [
                item for item in runtime_messages if item.get("role") == "tool"
            ]
            model_observations.append(copy.deepcopy(tool_messages))

            if len(tool_messages) == 0:
                return self._assistant_tool_message(
                    self._tool_call(
                        "fake_search",
                        {"keyword": "Agent Runtime"},
                        "call-search",
                    )
                )

            if len(tool_messages) == 1:
                return self._assistant_tool_message(
                    self._tool_call(
                        "fake_summarize",
                        {"source": "search-result"},
                        "call-summary",
                    )
                )

            return self._assistant_final_message("资料已搜索并整理。")

        def fake_tool(message, role):
            result = self._successful_fake_tool(message, role)
            executed_tools.append(result[3])
            return result

        with (
            patch.object(agent, "apply_skill", side_effect=self._no_skill),
            patch.object(agent, "call_model", side_effect=fake_model),
            patch.object(agent, "use_tool", side_effect=fake_tool),
        ):
            func_id, answer, trace, task_id = self._run_agent(
                "user-chain", "搜索 Agent Runtime 并整理摘要"
            )

        task = self._get_task(task_id)
        self.assertIsNone(func_id)
        self.assertEqual(answer, "资料已搜索并整理。")
        self.assertEqual(executed_tools, ["fake_search", "fake_summarize"])
        self.assertEqual(len(trace), 2)
        self.assertEqual(task["status"], "SUCCEEDED")
        self.assertEqual(
            [step["name"] for step in task["steps"] if step["type"] == "tool_call"],
            ["fake_search", "fake_summarize"],
        )
        # 第三轮模型调用时，历史中应已有两个工具 Observation。
        self.assertEqual(len(model_observations[-1]), 2)

    def test_03_tool_execution_failure(self):
        """工具失败：Agent 可以生成解释性回复，但任务状态必须保留 FAILED。"""

        def fake_failed_tool(message, role):
            tool_call = message["tool_calls"][0]
            function = tool_call["function"]
            arguments = json.loads(function["arguments"])
            return (
                tool_call["id"],
                "模拟工具执行失败：目标程序未启动",
                message,
                function["name"],
                arguments,
                "failed",
            )

        model_outputs = [
            self._assistant_tool_message(
                self._tool_call("fake_open_app", {"name": "不存在的程序"}, "call-fail")
            ),
            self._assistant_final_message("程序未能启动，任务没有完成。"),
        ]

        with (
            patch.object(agent, "apply_skill", side_effect=self._no_skill),
            patch.object(agent, "call_model", side_effect=model_outputs),
            patch.object(agent, "use_tool", side_effect=fake_failed_tool),
        ):
            func_id, answer, trace, task_id = self._run_agent(
                "user-failure", "打开一个不存在的程序"
            )

        task = self._get_task(task_id)
        self.assertIsNone(func_id)
        self.assertIn("没有完成", answer)
        self.assertEqual(trace[0]["status"], "failed")
        self.assertEqual(task["status"], "FAILED")
        self.assertEqual(
            task["stop_reason"],
            "tool_execution_failed_in_last_batch",
        )
        self.assertEqual(task["steps"][0]["status"], "FAILED")
        self.assertEqual(task["steps"][-1]["type"], "final_response")

    def test_04_user_cancellation(self):
        """复用 Krita 的命令处理：工具执行期间设置 Event，随后跳过同批剩余工具。"""
        user_id = "user-cancel"
        task_state = TaskState(user_id, "执行两个连续操作，然后测试中途取消")
        running_tasks = {user_id: task_state}
        executed_tools = []
        model_call_count = 0
        cancel_command_result = []

        def fake_model(model_type, runtime_messages, remaining):
            nonlocal model_call_count
            model_call_count += 1
            return self._assistant_tool_message(
                self._tool_call("fake_first", {"value": 1}, "call-first"),
                self._tool_call("fake_second", {"value": 2}, "call-second"),
            )

        def fake_tool(message, role):
            tool_call = message["tool_calls"][0]
            function = tool_call["function"]
            arguments = json.loads(function["arguments"])
            executed_tools.append(function["name"])

            # 模拟第一项工具正常执行期间，用户通过 Krita 风格命令处理器发送“停止”。
            # 命令处理器只设置 Event；当前工具继续执行并正常返回。
            handled, reply = handle_cancel_command("停止", user_id, running_tasks)
            cancel_command_result.append((handled, reply))
            return (
                tool_call["id"],
                f"{function['name']} 已执行",
                message,
                function["name"],
                arguments,
                "success",
            )

        with (
            patch.object(agent, "apply_skill", side_effect=self._no_skill),
            patch.object(agent, "call_model", side_effect=fake_model),
            patch.object(agent, "use_tool", side_effect=fake_tool),
        ):
            func_id, answer, trace, task_id = self._run_agent(
                user_id,
                task_state.description,
                task_state=task_state,
            )

        task = self._get_task(task_id)
        tool_steps = [
            item for item in task["steps"] if item["type"] == "tool_call"
        ]

        self.assertIsNone(func_id)
        self.assertIn("任务已取消", answer)
        self.assertEqual(executed_tools, ["fake_first"])
        self.assertEqual(model_call_count, 1)
        self.assertEqual(cancel_command_result[0][0], True)
        self.assertTrue(task_state.cancel_event.is_set())
        self.assertEqual(task_state.snapshot()["status"], "CANCELLED")
        self.assertEqual(task["status"], "CANCELLED")
        self.assertEqual(task["stop_reason"], "user_cancelled")
        self.assertEqual(
            [item["status"] for item in tool_steps],
            ["SUCCEEDED", "SKIPPED"],
        )
        self.assertEqual(
            [item["status"] for item in trace],
            ["success", "cancelled"],
        )

    def test_05_multi_user_concurrency(self):
        """
        多用户并发：两个用户同时进入 run_agent，检查任务记录不串用户。

        这是 Agent 运行层并发测试；不覆盖 QQ SDK 回调、用户锁和共享桌面
        工具上下文的端到端并发行为。
        """
        barrier = threading.Barrier(2)

        def concurrent_model(model_type, runtime_messages, remaining):
            user_text = next(
                item["content"]
                for item in runtime_messages
                if item.get("role") == "user"
            )
            # 两个任务都到达模型请求阶段后才继续，确保测试确实发生并发。
            barrier.wait(timeout=8)
            return self._assistant_final_message(f"处理完成：{user_text}")

        def run_for_user(user_id, text):
            return self._run_agent(user_id, text)

        with (
            patch.object(agent, "apply_skill", side_effect=self._no_skill),
            patch.object(agent, "call_model", side_effect=concurrent_model),
        ):
            with ThreadPoolExecutor(max_workers=2) as executor:
                future_a = executor.submit(
                    run_for_user, "user-A", "任务 A"
                )
                future_b = executor.submit(
                    run_for_user, "user-B", "任务 B"
                )
                result_a = future_a.result(timeout=15)
                result_b = future_b.result(timeout=15)

        task_a = self._get_task(result_a[3])
        task_b = self._get_task(result_b[3])

        self.assertEqual(result_a[1], "处理完成：任务 A")
        self.assertEqual(result_b[1], "处理完成：任务 B")
        self.assertEqual(task_a["status"], "SUCCEEDED")
        self.assertEqual(task_b["status"], "SUCCEEDED")
        self.assertEqual(task_a["user_id"], "user-A")
        self.assertEqual(task_b["user_id"], "user-B")
        self.assertNotEqual(task_a["task_id"], task_b["task_id"])

    def test_06_parameter_error_then_model_correction(self):
        """
        参数错误修正：第一次参数校验失败，模型读取错误 Observation 后修正参数。
        第二次调用成功后，最终任务应为 SUCCEEDED。
        """
        executed_arguments = []

        def fake_echo(value):
            executed_arguments.append(value)
            return {"status": "success", "message": value}

        model_outputs = [
            self._assistant_tool_message(
                self._tool_call(
                    "fake_echo",
                    {"wrong_field": "第一次错误参数"},
                    "call-invalid",
                )
            ),
            self._assistant_tool_message(
                self._tool_call(
                    "fake_echo",
                    {"value": "修正后的参数"},
                    "call-corrected",
                )
            ),
            self._assistant_final_message("参数已修正，工具执行成功。"),
        ]

        with (
            patch.object(agent, "apply_skill", side_effect=self._no_skill),
            patch.object(agent, "call_model", side_effect=model_outputs),
            patch.object(agent, "TOOL_REGISTRY", {"fake_echo": fake_echo}),
            patch.object(agent, "check_permission", return_value=True),
            patch.object(agent, "get_required_params", return_value=["value"]),
            patch.object(
                agent,
                "check_args",
                side_effect=lambda args, required, name: (
                    isinstance(args, dict) and "value" in args
                ),
            ),
        ):
            func_id, answer, trace, task_id = self._run_agent(
                "user-correction", "调用 fake_echo，参数 value 为修正后的参数"
            )

        task = self._get_task(task_id)
        self.assertIsNone(func_id)
        self.assertEqual(answer, "参数已修正，工具执行成功。")
        self.assertEqual([item["status"] for item in trace], ["failed", "success"])
        self.assertEqual(executed_arguments, ["修正后的参数"])
        self.assertEqual(task["status"], "SUCCEEDED")
        self.assertEqual(
            [step["status"] for step in task["steps"] if step["type"] == "tool_call"],
            ["FAILED", "SUCCEEDED"],
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
