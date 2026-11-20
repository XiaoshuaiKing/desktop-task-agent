"""状态投影：把上一个被中断的任务映射成几行，让模型知道"之前发生过什么"。

这是**真正的投影**——数据源是状态机的持久化形态（tasks 表），
不是循环变量。挑出 status / steps / stop_reason，映射成模型能读的文本。

为什么需要它：
    崩溃时本次任务的对话不会落库（save_session 只在任务结束后调用），
    所以 messages 里没有那段执行过程，模型对"上次崩了"完全无感。
    这些信息只剩在 tasks 表里——投影是把"存下来的状态"变成"模型能看到的状态"。

和"预算提示"的区别：
    预算提示 / 每轮 / 数据来自循环变量 → 管"当前任务还剩多少"
    状态投影 / 每任务一次 / 数据来自 tasks 表 → 管"上一个任务怎么了"
"""
from datetime import datetime, timedelta

from core.session_manager import DB_PATH
from core.task_manager import TaskManager

# 超过这个窗口的中断任务不再提醒：用户多半已经重新开始了，再提只会打扰。
INTERRUPT_WINDOW_HOURS = 24

# 结果未知的步骤：工具可能已经产生外部副作用，需要人工核对。
UNSETTLED = "EXECUTING"


def _parse_time(value):
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def find_interrupted_task(user_id, db_path=None, window_hours=INTERRUPT_WINDOW_HOURS):
    """找最近一条仍在该窗口内的中断任务；没有就返回 None。"""
    db_path = db_path or DB_PATH
    manager = TaskManager(db_path)
    try:
        tasks = manager.get_unfinished_tasks(user_id)
    except Exception:
        return None

    cutoff = datetime.now().astimezone() - timedelta(hours=window_hours)
    candidates = []
    for task in tasks:
        if task.get("status") != "INTERRUPTED":
            continue
        stamp = _parse_time(task.get("updated_at") or task.get("created_at"))
        if stamp is None or stamp >= cutoff:
            candidates.append(task)
    return candidates[0] if candidates else None


def summarize_steps(task):
    """统计步骤：成功多少、结果未知多少、失败多少。"""
    counts = {"SUCCEEDED": 0, UNSETTLED: 0, "FAILED": 0, "SKIPPED": 0}
    for step in (task or {}).get("steps") or []:
        status = step.get("status")
        if status in counts:
            counts[status] += 1
    return counts


def render_state(task):
    """把一个中断任务映射成给模型看的几行；无任务时返回空串。"""
    if not task:
        return ""

    desc = str(task.get("description") or "").replace("\n", " ")[:60]
    counts = summarize_steps(task)
    done = counts["SUCCEEDED"]
    unsettled = counts[UNSETTLED]
    failed = counts["FAILED"]

    lines = ["[上一个任务未完成] 执行中被中断（进程重启）。"]
    if desc:
        lines.append("原请求：%s" % desc)
    lines.append("进度：已完成 %d 步；结果未知 %d 步；失败 %d 步。" % (done, unsettled, failed))

    if unsettled:
        lines.append("注意：有 %d 步停在执行中，外部动作可能已经生效。"
                     "如需重做，先确认那几步是否已完成，避免重复执行。" % unsettled)
    else:
        lines.append("没有停在执行中的步骤，可以安全重试。")

    return "\n".join(lines)


def build_state_projection(user_id, db_path=None, task_manager=None,
                           window_hours=INTERRUPT_WINDOW_HOURS):
    """供 run_agent 调用：返回状态投影文本，没有中断任务时返回空串。

    任何异常都返回空串——投影失败不能拖垮新任务。
    """
    try:
        if task_manager is not None:
            interrupted = _find_with_manager(task_manager, user_id, window_hours)
        else:
            interrupted = find_interrupted_task(user_id, db_path=db_path,
                                                window_hours=window_hours)
        return render_state(interrupted)
    except Exception:
        return ""


def _find_with_manager(manager, user_id, window_hours):
    """复用传入的 TaskManager，避免重复连接。"""
    try:
        tasks = manager.get_unfinished_tasks(user_id)
    except Exception:
        return None
    cutoff = datetime.now().astimezone() - timedelta(hours=window_hours)
    for task in tasks:
        if task.get("status") != "INTERRUPTED":
            continue
        stamp = _parse_time(task.get("updated_at") or task.get("created_at"))
        if stamp is None or stamp >= cutoff:
            return task
    return None
