"""Agent 运行时任务状态。

该类复用 Krita Agent 的协作式取消设计：
- Bot 与 Agent 共享同一个 threading.Event；
- Bot 收到取消命令时只设置事件，不强制终止工作线程；
- Agent 在模型请求边界和工具派发边界检查事件。

它只管理内存中的运行时状态，不替代 core.task_manager.TaskManager 的 SQLite 持久化职责。
"""

import threading
import time
import uuid


class TaskState:
    """记录单个用户当前任务的运行状态，并提供线程安全的取消信号。"""

    def __init__(self, user_id, description, cancel_event=None):
        self.task_id = uuid.uuid4().hex[:12]
        self.user_id = str(user_id)
        self.description = str(description)

        # 运行时状态；数据库中的最终任务状态仍由 TaskManager 保存。
        self.status = "PENDING"
        self.phase = "waiting"
        self.current_tool = None
        self.last_tool = None
        self.tool_count = 0
        self.model_rounds = 0
        self.max_steps = None
        self.timeout = None
        self.stop_reason = None
        self.started_at = time.monotonic()

        # threading.Event 可在线程池中的 run_agent 与 Bot 异步回调之间安全传递。
        self.cancel_event = cancel_event or threading.Event()
        self.lock = threading.Lock()

    def update(self, **kwargs):
        """线程安全地更新任务阶段、状态和当前工具等字段。"""
        with self.lock:
            for key, value in kwargs.items():
                setattr(self, key, value)

    def model_round_started(self):
        """线程安全地增加一次模型请求轮次。"""
        with self.lock:
            self.model_rounds += 1

    def tool_finished(self, name, status="success"):
        """记录一个已经返回的工具；被取消而跳过的工具不调用本方法。"""
        with self.lock:
            self.tool_count += 1
            self.current_tool = name
            self.last_tool = name

    def snapshot(self):
        """返回线程安全的状态快照，便于日志或后续进度查询使用。"""
        with self.lock:
            return {
                "task_id": self.task_id,
                "user_id": self.user_id,
                "description": self.description,
                "status": self.status,
                "phase": self.phase,
                "current_tool": self.current_tool,
                "last_tool": self.last_tool,
                "tool_count": self.tool_count,
                "model_rounds": self.model_rounds,
                "max_steps": self.max_steps,
                "timeout": self.timeout,
                "stop_reason": self.stop_reason,
                "elapsed": round(time.monotonic() - self.started_at, 2),
                "cancel_requested": self.cancel_event.is_set(),
            }
