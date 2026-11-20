"""QQ Agent 的任务取消命令处理。

取消命令必须在获取用户级 asyncio.Lock 之前处理，否则正在运行的任务会持有该锁，
导致取消消息只能排队，无法及时设置取消事件。
"""

STOP_COMMANDS = {
    "停止",
    "取消",
    "取消任务",
    "停止任务",
    "停止绘图",
    "cancel",
    "/cancel",
    "stop",
}


def is_cancel_command(content):
    """判断输入是否为独立取消命令。"""
    command = (content or "").strip().rstrip("！？?!。 ").lower()
    return command in STOP_COMMANDS


def handle_cancel_command(content, user_id, running_tasks):
    """设置指定用户当前任务的取消事件，返回 (是否处理, 回复文本)。"""
    if not is_cancel_command(content):
        return False, None

    task_state = running_tasks.get(user_id)
    if task_state is None:
        return True, "当前没有正在执行的任务。"

    if task_state.cancel_event.is_set():
        return True, "取消请求已经收到，正在等待当前调用结束。"

    # 只发送协作式取消信号，不尝试强制杀死正在执行的工具线程。
    task_state.cancel_event.set()
    return True, "已收到取消请求。当前正在执行的工具不会被强制中断；它结束后将停止后续操作。"
