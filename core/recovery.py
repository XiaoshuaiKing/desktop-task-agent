"""中断检测：找出进程重启后被遗留在中间态的任务，标记为 INTERRUPTED。

范围说明（很重要）：
- 本模块只做「发现与标记」，不做恢复。恢复需要按步骤对账（哪些已完成、
  哪些从未执行、哪些结果未知），那需要工具幂等声明，属于下一阶段。
- 标记完不重跑任何任务：重跑可能让已经生效的外部动作再发生一次。
"""

from core.session_manager import DB_PATH, init_db
from core.task_manager import TaskManager

# 只有这几种状态代表「任务曾开始但没走到终态」。
# INTERRUPTED 不在这里：已经标记过的任务不应被反复改写。
STALE_STATUSES = ("PENDING", "RUNNING", "WAITING_CONFIRMATION")

# 步骤停在执行中，说明工具可能已经产生外部副作用，属于「结果未知」。
UNSETTLED_STEP = "EXECUTING"


def find_stale_tasks(db_path=None, user_id=None):
    """只读：返回遗留任务列表，不修改数据库。"""
    db_path = db_path or DB_PATH
    init_db(db_path)
    manager = TaskManager(db_path)
    return [
        task for task in manager.get_unfinished_tasks(user_id)
        if task["status"] in STALE_STATUSES
    ]


def summarize_steps(task):
    """统计一个任务的步骤分布，用来判断恢复时该跳过还是该核对。"""
    counts = {}
    for step in task.get("steps") or []:
        status = step.get("status")
        counts[status] = counts.get(status, 0) + 1
    return counts


def mark_stale_interrupted(db_path=None, user_id=None, dry_run=False):
    """把遗留任务标记为 INTERRUPTED，返回一份可供日志/自检使用的报告。

    dry_run=True 时只扫描不写入，用于 --check 这类应当无副作用的入口。
    """
    db_path = db_path or DB_PATH
    stale = find_stale_tasks(db_path=db_path, user_id=user_id)

    report = {
        "db_path": str(db_path),
        "dry_run": bool(dry_run),
        "scanned": len(stale),
        "marked": [],
        "failed": [],
        "task_status_counts": {},
        "steps_executing": 0,
    }

    for task in stale:
        status = task["status"]
        report["task_status_counts"][status] = (
            report["task_status_counts"].get(status, 0) + 1
        )
        report["steps_executing"] += summarize_steps(task).get(UNSETTLED_STEP, 0)

    if dry_run:
        report["marked"] = [task["task_id"] for task in stale]
        return report

    manager = TaskManager(db_path)
    for task in stale:
        task_id = task["task_id"]
        try:
            manager.update_task_status(
                task_id, "INTERRUPTED", stop_reason="process_restarted"
            )
            report["marked"].append(task_id)
        except Exception as exc:  # 数据库本身异常时不要中断启动流程
            report["failed"].append({"task_id": task_id, "error": str(exc)})

    return report


def format_report(report):
    """把报告压成一行，供启动日志使用。"""
    if not report["scanned"]:
        return "中断检测：无遗留任务"
    parts = [
        "中断检测：扫描到 %d 个遗留任务" % report["scanned"],
        "状态分布 %s" % (report["task_status_counts"] or "无"),
    ]
    if report["dry_run"]:
        parts.append("dry-run（未写入）")
    else:
        parts.append("已标记 %d 个为 INTERRUPTED" % len(report["marked"]))
        if report["failed"]:
            parts.append("失败 %d 个" % len(report["failed"]))
    if report["steps_executing"]:
        parts.append("其中 %d 个步骤停在 EXECUTING（结果未知，待核对）" % report["steps_executing"])
    return "；".join(parts)
