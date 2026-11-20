"""预算提示：每一轮告诉模型"还剩多少、该不该收手"。

名字说明（很重要）：
    这**不是投影**。数据源是 run_agent 里的循环变量（step / remaining / max_steps），
    不是状态机。它做的是"格式化"，不是"从大结构映射出子集"。
    真正的状态投影在 core/task_state_projection.py——那个才从 tasks 表映射。

设计边界：只给 messages 给不了的信息。
    "做了什么"由 messages 提供；这里只补剩余预算、步数上限、以及收尾建议。
"""

# 剩余时间低于这个秒数、或步数超过上限的这个比例，就提醒模型收尾。
TIME_WARN_SECONDS = 45
STEP_WARN_RATIO = 0.8


def render_budget(step, max_steps, remaining_seconds):
    """返回给模型看的预算块；信息不足时返回空串。

    step 只用于判断"是否临近上限"，不作为数据项投出去——
    第几步模型能从 messages 数出来。
    """
    if not max_steps:
        return ""

    lines = ["[预算] 剩余约 %d 秒；步数上限 %d。" % (max(0, int(remaining_seconds or 0)), int(max_steps))]

    near_time = remaining_seconds is not None and remaining_seconds <= TIME_WARN_SECONDS
    near_steps = bool(max_steps) and step >= max_steps * STEP_WARN_RATIO
    if near_time or near_steps:
        lines.append("提示：预算临近上限，优先收尾并给出已完成的结论，不要开新步骤。")

    return "\n".join(lines)


def build_budget_block(step, max_steps, remaining_seconds):
    """供 run_agent 每轮调用。不查库、不抛异常。"""
    try:
        return render_budget(step, max_steps, remaining_seconds)
    except Exception:
        return ""
