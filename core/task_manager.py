
import json
import sqlite3
import uuid
from pathlib import Path
from datetime import datetime
from contextlib import contextmanager

TASK_STATUSES = {
    "PENDING",
    "RUNNING",
    "WAITING_CONFIRMATION",
    "SUCCEEDED",
    "FAILED",
    "CANCELLED",
    "TIMED_OUT",
    "INTERRUPTED"
}

STEP_TYPES = {
    "tool_call",
    "final_response",
    # 上下文压缩也记成一步：不记录就无法回答"压缩会不会让任务变差"。
    "context_compression"
}

STEP_STATUSES = {
    "PENDING",
    "EXECUTING",
    "SUCCEEDED",
    "FAILED",
    "UNKNOWN",
    "SKIPPED"
}

# 最终回复的投递状态与 tasks.status（任务执行状态）相互独立。
REPLY_STATUSES = {
    "NOT_SENT",
    "SENDING",
    "SENT",
    "FAILED",
    "UNKNOWN"
}

# 获取当前时间，并转换成字符串，用于记录任务创建、更新和执行的时间。
def get_timestamp():
    return datetime.now().astimezone().isoformat(timespec="seconds")

class TaskManager:
    def __init__(self, db_path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    def _connect(self):
        conn = sqlite3.connect(str(self.db_path), timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _transaction(self):
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def create_task(self, user_id, description):
        """创建一条新的任务记录"""
        task_id = uuid.uuid4().hex
        now = get_timestamp()

        with self._transaction() as conn:
            conn.execute("""
                INSERT INTO tasks(
                    task_id,user_id,description,status,
                    steps,stop_reason,created_at,updated_at
                )
                VALUES(?,?,?,?,?,?,?,?)
            """, (
                task_id,
                str(user_id),
                description,
                "PENDING",
                "[]",
                None,
                now,
                now
            ))

        return task_id

    def update_task_status(self, task_id, status, stop_reason=None):
        """更新整个任务的状态"""
        if status not in TASK_STATUSES:
            raise ValueError(f"Invalid task status: {status}")

        with self._transaction() as conn:
            cursor = conn.execute("""
                UPDATE tasks
                SET status=?,stop_reason=?,updated_at=?
                WHERE task_id=?
            """, (status, stop_reason, get_timestamp(), task_id))

            if cursor.rowcount == 0:
                raise KeyError(f"Task not found: {task_id}")

    def add_step(self, task_id, step_type, name,
                 arguments=None, tool_call_id=None):
        """增加一个工具调用或最终回复步骤"""
        if step_type not in STEP_TYPES:
            raise ValueError(f"Invalid step type: {step_type}")
        step_id = uuid.uuid4().hex[:12]
        now = get_timestamp()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT steps FROM tasks WHERE task_id=?",
                (task_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Task not found: {task_id}")
            steps = json.loads(row["steps"] or "[]")
            step = {
                "step_id": step_id,
                "step_index": len(steps) + 1,
                "type": step_type,
                "name": name,
                "tool_call_id": tool_call_id,
                "arguments": arguments,
                "status": "PENDING",
                "result": None,
                "error": None,
                "created_at": now,
                "started_at": None,
                "finished_at": None
            }
            steps.append(step)
            conn.execute("""
                UPDATE tasks
                SET steps=?,updated_at=?
                WHERE task_id=?
            """, (
                json.dumps(steps, ensure_ascii=False, default=str),
                now,
                task_id
            ))
        return step_id

    def add_tool_call(self, task_id, tool_name, arguments,
                      tool_call_id=None):
        """记录一次工具调用"""
        return self.add_step(
            task_id=task_id,
            step_type="tool_call",
            name=tool_name,
            arguments=arguments,
            tool_call_id=tool_call_id
        )

    def update_step(self, task_id, step_id, status,
                    result=None, error=None):
        """更新某个步骤的执行状态和结果"""
        if status not in STEP_STATUSES:
            raise ValueError(f"Invalid step status: {status}")
        now = get_timestamp()
        with self._transaction() as conn:
            row = conn.execute(
                "SELECT steps FROM tasks WHERE task_id=?",
                (task_id,)
            ).fetchone()

            if row is None:
                raise KeyError(f"Task not found: {task_id}")

            steps = json.loads(row["steps"] or "[]")
            found = False
            for step in steps:
                if step["step_id"] == step_id:
                    step["status"] = status

                    if status == "EXECUTING" and step["started_at"] is None:
                        step["started_at"] = now

                    if status in {"SUCCEEDED", "FAILED", "UNKNOWN", "SKIPPED"}:
                        step["finished_at"] = now

                    if result is not None:
                        step["result"] = result

                    if error is not None:
                        step["error"] = str(error)

                    found = True
                    break

            if not found:
                raise KeyError(f"Step not found: {step_id}")
            conn.execute("""
                UPDATE tasks
                SET steps=?,updated_at=?
                WHERE task_id=?
            """, (
                json.dumps(steps, ensure_ascii=False, default=str),
                now,
                task_id
            ))

    def record_final_response(self, task_id, content):
        """记录Agent最终回复"""
        step_id = self.add_step(
            task_id=task_id,
            step_type="final_response",
            name="final_response"
        )
        self.update_step(
            task_id=task_id,
            step_id=step_id,
            status="SUCCEEDED",
            result=content
        )
        return step_id

    def update_reply_status(self, task_id, status, error=None):
        """
        更新最终回复的投递状态。

        注意：
        - 本方法只更新 reply_status，不改变任务执行状态 status。
        - SENT 表示 QQ 消息发送 API 正常返回，不代表用户已经阅读。
        - UNKNOWN 表示请求超时或连接中断，无法确认平台是否已经接收。
        """
        if status not in REPLY_STATUSES:
            raise ValueError(f"Invalid reply status: {status}")

        now = get_timestamp()

        if status == "SENT":
            reply_error = None
            reply_sent_at = now
        elif status in {"NOT_SENT", "SENDING"}:
            reply_error = None
            reply_sent_at = None
        else:
            reply_error = str(error)[:1000] if error is not None else None
            reply_sent_at = None

        with self._transaction() as conn:
            cursor = conn.execute("""
                UPDATE tasks
                SET reply_status=?,
                    reply_error=?,
                    reply_sent_at=?,
                    updated_at=?
                WHERE task_id=?
            """, (
                status,
                reply_error,
                reply_sent_at,
                now,
                task_id
            ))

            if cursor.rowcount == 0:
                raise KeyError(f"Task not found: {task_id}")

    def finish_task(self, task_id, status, stop_reason=None):
        """结束任务"""
        if status not in {
            "SUCCEEDED", "FAILED", "CANCELLED",
            "TIMED_OUT", "INTERRUPTED"
        }:
            raise ValueError(f"Invalid final status: {status}")
        self.update_task_status(task_id, status, stop_reason)

    def get_task(self, task_id):
        """查询完整任务及其所有步骤"""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM tasks WHERE task_id=?",
                (task_id,)
            ).fetchone()

            if row is None:
                return None

            task = dict(row)
            task["steps"] = json.loads(task["steps"] or "[]")
            return task
        finally:
            conn.close()

    def get_unfinished_tasks(self, user_id=None):
        """查询尚未结束的任务"""
        statuses = (
            "PENDING", "RUNNING",
            "WAITING_CONFIRMATION", "INTERRUPTED"
        )

        conn = self._connect()
        try:
            if user_id is None:
                rows = conn.execute("""
                    SELECT * FROM tasks
                    WHERE status IN (?,?,?,?)
                    ORDER BY created_at
                """, statuses).fetchall()
            else:
                rows = conn.execute("""
                    SELECT * FROM tasks
                    WHERE user_id=? AND status IN (?,?,?,?)
                    ORDER BY created_at
                """, (str(user_id), *statuses)).fetchall()

            tasks = []
            for row in rows:
                task = dict(row)
                task["steps"] = json.loads(task["steps"] or "[]")
                tasks.append(task)

            return tasks
        finally:
            conn.close()
