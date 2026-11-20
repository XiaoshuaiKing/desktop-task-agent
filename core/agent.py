import json
import time
import copy
import re
from tools.permissions import check_permission,check_args,get_required_params
from models.local_client import call_qwen
from models.llm_client import call_llm
from core.skill_manager import select_skill,load_skill
from tools.tool_schemas import TOOL_REGISTRY
from core.session_manager import DB_PATH
from core.task_manager import TaskManager
from core.task_state import TaskState
from core.memory import build_memory_block, load_pinned
from core.context_builder import build_context
from core.task_budget import build_budget_block
from core.task_state_projection import build_state_projection
from tools.tools import bind_task_context

def call_model(model_type,messages,remaining):
    end_time = time.time()+remaining
    for attempt in range(2):
        remaining = end_time - time.time()
        if remaining<=0:
            return {
                "role":"assistant",
                "content":"模型请求超时",
                "tool_calls":[],
                "_error":"timeout"
            }
        try:
            if model_type=="local":
                return call_qwen(messages,remaining)
            if model_type=="api":
                return call_llm(messages,remaining)
            raise ValueError(f"unknown model type:{model_type}")
        except TimeoutError:
            if attempt==1:
                return {
                    "role":"assistant",          
                    "content":"模型请求超时",
                    "tool_calls":[],
                    "_error":"timeout"
                }

def use_tool(message,role):
    if message.get("tool_calls"):
        tool_call=message["tool_calls"][0]
        func_id=tool_call["id"]
        func_name=tool_call["function"]["name"]
        func=TOOL_REGISTRY.get(func_name)
        if func is None:
            return func_id,"工具不存在",message,func_name,{},"failed"
        tool_fig=check_permission(func_name,role)
        if tool_fig:
            args={}
            status="success"
            try:
                args=json.loads(tool_call["function"]["arguments"])
                required=get_required_params(func_name)
                args_fig=check_args(args,required,func_name)
                if args_fig:
                    answer=func(**args)
                    try:
                        tool_result=json.loads(answer) if isinstance(answer,str) else answer
                        if isinstance(tool_result,dict) and tool_result.get("status")=="error":
                            status="failed"
                    except (json.JSONDecodeError,TypeError):
                        pass
                else:
                    answer="参数不匹配"
                    status="failed"
            except Exception as e:
                answer=f"工具执行失败：{e}"
                status="failed"
            return func_id,answer,message,func_name,args,status
        else:
            return func_id,"用户无权限",message,func_name,{},"failed"
    return None,message.get("content",""),None,None,{},"success"

def _cancel_requested(task_state):
    """判断当前任务是否收到用户取消请求。"""
    return task_state is not None and task_state.cancel_event.is_set()


def execute_tool_calls(message, role, task_manager, task_id, task_state=None):
    """按模型返回顺序串行执行工具；取消后将未派发工具持久化为 SKIPPED。"""
    results = []
    for tool_call in message.get("tool_calls", []):
        single_message = message.copy()
        single_message["tool_calls"] = [tool_call]

        # 取得工具名称和参数，用于保存调用记录。
        tool_name = tool_call["function"]["name"]
        try:
            arguments = json.loads(tool_call["function"]["arguments"])
        except (json.JSONDecodeError, TypeError):
            arguments = {}

        # 先记录工具调用。即使任务随后被取消，也能在数据库中看到未执行步骤。
        step_id = task_manager.add_tool_call(
            task_id, tool_name, arguments, tool_call["id"]
        )

        # 第一次检查：如果工具还没开始，直接标记 SKIPPED，不调用真实工具。
        if _cancel_requested(task_state):
            skipped_message = "用户已取消任务，该工具未执行。"
            task_manager.update_step(
                task_id, step_id, "SKIPPED",
                result=skipped_message, error=skipped_message
            )
            results.append((
                tool_call["id"], skipped_message, single_message,
                tool_name, arguments, "cancelled"
            ))
            continue

        # 先持久化 EXECUTING，再做一次派发前检查，缩小检查与调用之间的竞态窗口。
        task_manager.update_step(task_id, step_id, "EXECUTING")
        if _cancel_requested(task_state):
            skipped_message = "用户已取消任务，该工具未执行。"
            task_manager.update_step(
                task_id, step_id, "SKIPPED",
                result=skipped_message, error=skipped_message
            )
            results.append((
                tool_call["id"], skipped_message, single_message,
                tool_name, arguments, "cancelled"
            ))
            continue

        if task_state is not None:
            task_state.update(phase="executing", current_tool=tool_name)

        # 复用原有单工具执行函数。当前工具开始后收到取消信号时，不强制中断它。
        result = use_tool(single_message, role)
        if task_state is not None:
            task_state.tool_finished(tool_name, status=result[-1])

        # 将原有的小写执行状态转换为数据库使用的大写状态。
        db_status = "SUCCEEDED" if result[-1] == "success" else "FAILED"
        task_manager.update_step(
            task_id,
            step_id,
            db_status,
            result=str(result[1]),
            error=None if db_status == "SUCCEEDED" else str(result[1])
        )
        results.append(result)

    return results

def apply_skill(model_type,messages,call_model):
    runtime_messages=copy.deepcopy(messages)
    user_input=messages[-1]["content"]
    skill_name=select_skill(model_type,user_input,call_model)
    if skill_name:
        skill_content=load_skill(skill_name)
        runtime_messages[0]["content"]+=f"\n\n当前任务使用以下Skill：\n{skill_content}"
    return runtime_messages


_WEB_TOOLS = {
    "list_browser_tabs", "open_website", "smart_web_operate", "extract_web_content",
    "click_web_element", "close_website",
}
_BROWSER_APPS = {"浏览器", "chrome", "google chrome", "chrome.exe", "谷歌浏览器",
                 "edge", "msedge", "msedge.exe", "microsoft edge", "微软浏览器"}
_WEB_APPS = {"deepseek": "https://chat.deepseek.com", "豆包": "https://www.doubao.com",
             "kimi": "https://kimi.moonshot.cn", "通义千问": "https://tongyi.aliyun.com",
             "文心一言": "https://yiyan.baidu.com"}

def _requested_browser(user_text, default="edge"):
    """仅从本轮用户的浏览器指令选择 Chrome；搜索词、网页结果和模型历史不决定浏览器。

    default 用于「本轮没提」的情况：传已保存的偏好，而不是硬编码 edge。
    优先级：本轮明确选择 → 已存偏好 → edge。
    """
    chrome = r"(?:google\s+chrome|chrome(?:浏览器)?|谷歌浏览器)(?![a-z0-9.])"
    edge = r"(?:microsoft\s+edge|msedge|edge(?:浏览器)?|微软浏览器)(?![a-z0-9.])"
    text = user_text.strip().lower()
    # 识别常用中文操作指令；含否定的选择按 Edge 处理。
    if re.search(r"(?:别|不要|不用|禁止|不想)(?:再|去|使用|打开|用|切换到|在|启动|选择|换成|换用|切到|\s)*" + chrome, text):
        return "edge"
    choose = r"(?:^|[，,。；;！!\s])(?:请|帮我|麻烦|这次|改为|改成|就|直接|你|先)*"
    action = r"(?:使用|用|在|打开|启动|切换到|换成|换用|切到|选择)\s*"
    # 本轮明确点名 edge 时，优先级高于已存偏好。
    if re.search(choose + action + edge, text) or re.fullmatch(edge, text):
        return "edge"
    if re.search(choose + action + chrome, text) or re.fullmatch(chrome, text):
        return "chrome"
    return default

def _bind_browser_calls(tool_calls, browser_name):
    """派发前绑定实际浏览器，让模型上下文、任务步骤和 trace 都记录执行参数。"""
    for call in tool_calls:
        fn = call["function"]
        try:
            args = json.loads(fn["arguments"])
        except (TypeError, ValueError):
            continue  # 参数错误交给原执行器处理。
        if not isinstance(args, dict):
            continue
        if fn["name"] == "open_software" and isinstance(args.get("name"), str):
            # 网页快捷方式不能绕过浏览器选择而交给 Windows 默认浏览器。
            name = args["name"].strip().lower()
            url = _WEB_APPS.get(name)
            if not url and name.startswith(("http://", "https://")):
                url = args["name"].strip()
            if url:
                fn["name"] = "open_website"
                args = {"url": url}
            elif name in _BROWSER_APPS:
                args["name"] = "edge" if browser_name == "edge" else "chrome"
                fn["arguments"] = json.dumps(args, ensure_ascii=False)
        if fn["name"] in _WEB_TOOLS:
            args["browser_name"] = browser_name
            fn["arguments"] = json.dumps(args, ensure_ascii=False)


def _format_agent_result(func_id, answer, trace, task_id, return_task_id):
    """
    统一整理 Agent 返回值。

    默认仍返回原有三元组，避免破坏其他调用方；
    QQ 消息发送层可通过 return_task_id=True 获取 task_id。
    """
    if return_task_id:
        return func_id, answer, trace, task_id
    return func_id, answer, trace


def run_agent(model_type,messages,session_id,role,max_steps=25,timeout=250,return_task_id=False,task_state=None,cancel_event=None):
    start_time = time.time()
    step = 0
    trace = []

    # 兼容直接调用 run_agent(cancel_event=...) 的旧测试或调用方；
    # Bot 正常运行时传入完整 TaskState，以便管理运行时状态和取消事件。
    if task_state is None and cancel_event is not None:
        task_state = TaskState(
            session_id,
            messages[-1]["content"],
            cancel_event=cancel_event
        )
    elif task_state is not None and cancel_event is not None:
        # 同时传入时以 TaskState 中的事件为准，避免出现两个取消信号源。
        cancel_event = task_state.cancel_event

    # 初始化任务管理器，使用与 Session 相同的 SQLite 数据库。
    task_manager = TaskManager(DB_PATH)

    # 创建任务：一次用户请求对应一条任务记录。
    task_id = task_manager.create_task(
        user_id=session_id,
        description=messages[-1]["content"]
    )

    # 绑定可信的任务与用户身份：需要校验归属的工具（如 read_task_original）
    # 只能读当前任务，不能凭模型给的 task_id 去读别人的任务。
    bind_task_context(task_id, session_id)

    # 记录最近一批工具调用是否存在失败。
    # 后续批次全部成功时，可视为 Agent 已经通过重试/替代路径恢复。
    last_tool_batch_failed = False

    def finish_cancelled():
        """统一结束取消任务；已完成的工具副作用不会自动回滚。"""
        task_manager.finish_task(
            task_id,
            "CANCELLED",
            stop_reason="user_cancelled"
        )
        if task_state is not None:
            task_state.update(
                status="CANCELLED",
                phase="finished",
                current_tool=None,
                stop_reason="user_cancelled"
            )
        answer = (
            "任务已取消。当前已经开始的工具已正常结束，后续操作未继续执行；"
            "已完成的操作不会自动回滚。"
            if step > 0 else "任务已取消，未继续执行后续操作。"
        )
        return _format_agent_result(
            None, answer, trace, task_id, return_task_id
        )

    def guarded_call_model(current_model_type, current_messages, remaining_time):
        """在模型请求前后检查取消；不强制中断正在进行的网络/本地推理请求。"""
        if _cancel_requested(task_state):
            raise InterruptedError("用户取消任务")
        response = call_model(current_model_type, current_messages, remaining_time)
        if _cancel_requested(task_state):
            raise InterruptedError("用户取消任务")
        return response

    try:
        # 更新整体任务状态和运行时状态。
        task_manager.update_task_status(task_id, "RUNNING")
        if task_state is not None:
            task_state.update(
                status="RUNNING",
                phase="planning",
                current_tool=None,
                max_steps=max_steps,
                timeout=timeout
            )

        # 在 Skill 选择前检查；Skill 选择过程中也使用带取消检查的模型包装函数。
        if _cancel_requested(task_state):
            return finish_cancelled()

        browser_name = _requested_browser(
            messages[-1]["content"],
            default=(load_pinned(session_id) or {}).get("browser") or "edge",
        )
        runtime_messages = apply_skill(
            model_type, messages, guarded_call_model
        )

        # 长期记忆：只把结构化事实与少量历史任务拼进 system prompt。
        # 注意两点：① 它不进入 messages，因此不会被 trim_session 裁掉；
        #          ② 用户改口时 extract_pinned 覆盖旧值，不追加。
        memory_block = build_memory_block(session_id, messages[-1]["content"])
        if memory_block:
            runtime_messages[0]["content"] += "\n\n" + memory_block

        # 状态投影：上一个任务如果被中断，把"做到哪、哪步结果未知"告诉模型。
        # 崩溃时那段对话不会落库，只能从 tasks 表映射出来。
        state_block = build_state_projection(session_id, task_manager=task_manager)
        if state_block:
            runtime_messages[0]["content"] += "\n\n" + state_block

        # 注入记忆与状态后的 system 原文。每轮预算提示都从它重建，避免逐轮累积。
        system_base = runtime_messages[0]["content"]

        # 目标锚点：**本轮**的 user 消息，不是会话第一条。
        # 多轮会话里保护历史请求，会把当前请求挤掉——评审已复现。
        goal_index = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=None)
        goal_text = str(messages[goal_index].get("content", "")) if goal_index is not None else ""

        # Skill 选择本身可能请求模型，完成后再次检查取消状态。
        if _cancel_requested(task_state):
            return finish_cancelled()

        while step < max_steps:
            # 每轮开始时检查取消，确保不会在取消后继续请求下一轮模型。
            if _cancel_requested(task_state):
                return finish_cancelled()

            if task_state is not None:
                task_state.update(phase="planning", current_tool=None)

            elapsed = time.time() - start_time
            remaining = timeout - elapsed

            # Agent 总执行时间超过限制，记录为超时任务。
            if elapsed >= timeout:
                task_manager.finish_task(
                    task_id,
                    "TIMED_OUT",
                    stop_reason="react_timeout"
                )
                if task_state is not None:
                    task_state.update(status="TIMED_OUT", phase="finished", stop_reason="react_timeout")
                return _format_agent_result(
                    None,
                    f"ReAct 循环超时（已用 {elapsed:.2f} 秒）",
                    trace,
                    task_id,
                    return_task_id
                )

            if task_state is not None:
                task_state.model_round_started()

            # 上下文压缩：按 token 预算重建本轮输入。目标锚点与配对结构受保护。
            runtime_messages, _compress_record = build_context(
                runtime_messages,
                goal_text=goal_text,
                goal_index=goal_index,
                task_manager=task_manager,
                task_id=task_id,
            )

            # 预算兜底：压到极限仍超限就明确停止，不发一个必然被模型拒绝的请求。
            if _compress_record.over_budget:
                task_manager.finish_task(
                    task_id, "FAILED", stop_reason="context_budget_exceeded"
                )
                if task_state is not None:
                    task_state.update(status="FAILED", phase="finished",
                                      stop_reason="context_budget_exceeded")
                return _format_agent_result(
                    None,
                    "上下文已超出预算（压缩后仍超限），任务停止而不是发送不完整请求。",
                    trace, task_id, return_task_id
                )

            # 预算提示：只补 messages 给不了的东西（剩余时间、步数上限、收尾建议）。
            # 每轮从 system_base 重建，避免逐轮累积。
            if runtime_messages and runtime_messages[0].get("role") == "system":
                budget = build_budget_block(step, max_steps, remaining)
                runtime_messages[0]["content"] = (
                    system_base + "\n\n" + budget if budget else system_base
                )

            # 请求 LLM；模型请求返回后立即检查取消，避免处理已经过期的工具计划。
            message = guarded_call_model(model_type, runtime_messages, remaining)
            tool_calls = message.get("tool_calls") or []

            if not tool_calls:
                answer = message.get("content", "")

                # call_model 通过内部标记区分超时，避免把超时提示当作成功回复。
                if message.get("_error") == "timeout":
                    task_manager.finish_task(
                        task_id,
                        "TIMED_OUT",
                        stop_reason="model_timeout"
                    )
                    if task_state is not None:
                        task_state.update(status="TIMED_OUT", phase="finished", stop_reason="model_timeout")
                    return _format_agent_result(
                        None, answer, trace, task_id, return_task_id
                    )

                # 最终回复步骤表示模型已经生成回复，不等同于 QQ 已经发送。
                task_manager.record_final_response(task_id, answer)

                # 记录最终结果前再次检查，避免把已收到的取消请求记成成功。
                if _cancel_requested(task_state):
                    return finish_cancelled()

                # 如果最近一批工具调用仍有失败，则任务失败，但仍返回解释性回复。
                # bot.py 可以继续发送该回复，并独立记录 reply_status=SENT。
                if last_tool_batch_failed:
                    task_manager.finish_task(
                        task_id,
                        "FAILED",
                        stop_reason="tool_execution_failed_in_last_batch"
                    )
                    if task_state is not None:
                        task_state.update(status="FAILED", phase="finished", stop_reason="tool_execution_failed_in_last_batch")
                else:
                    task_manager.finish_task(task_id, "SUCCEEDED")
                    if task_state is not None:
                        task_state.update(status="SUCCEEDED", phase="finished", current_tool=None)

                return _format_agent_result(
                    None, answer, trace, task_id, return_task_id
                )

            # 取消优先于 max_steps 判断：若模型请求期间收到取消，剩余工具统一记为 SKIPPED。
            if (
                not _cancel_requested(task_state)
                and step + len(tool_calls) > max_steps
            ):
                task_manager.finish_task(
                    task_id,
                    "FAILED",
                    stop_reason="max_steps_exceeded"
                )
                if task_state is not None:
                    task_state.update(status="FAILED", phase="finished", stop_reason="max_steps_exceeded")
                return _format_agent_result(
                    "max_steps_exceeded",
                    "工具调用次数过多，已终止",
                    trace,
                    task_id,
                    return_task_id
                )

            _bind_browser_calls(tool_calls, browser_name)

            # 原始 assistant 消息只追加一次，其中可能包含多个 tool_call。
            runtime_messages.append(message)
            messages.append(message)

            # 按模型返回顺序串行执行；取消后剩余工具写入 SKIPPED，不再调用真实工具。
            tool_results = execute_tool_calls(
                message,
                role,
                task_manager,
                task_id,
                task_state=task_state
            )

            # 取消跳过不属于工具失败；只有真实执行失败才影响任务失败判定。
            last_tool_batch_failed = any(
                result[-1] == "failed"
                for result in tool_results
            )

            for func_id, result, _, func_name, args, status in tool_results:
                # SKIPPED 工具不占用实际执行预算；trace 仍保留对应模型调用。
                if status != "cancelled":
                    step += 1

                error_t = result if status in {"failed", "cancelled"} else None
                trace.append({
                    "session_id": session_id,
                    "step": len(trace) + 1,
                    "action": func_name,
                    "args": args,
                    "observation": result,
                    "status": status,
                    "error": error_t
                })

                # 每个 tool_call 都补齐对应的 tool 消息，避免模型上下文出现未配对调用。
                tool_msg = {
                    "role": "tool",
                    "tool_call_id": func_id,
                    "content": str(result)
                }
                runtime_messages.append(tool_msg)
                messages.append(tool_msg)

            # 当前工具正常返回后检查取消；若已取消，不再请求下一轮模型。
            if _cancel_requested(task_state):
                return finish_cancelled()

        # while 循环因达到 max_steps 而结束时，记录任务失败。
        task_manager.finish_task(
            task_id,
            "FAILED",
            stop_reason="max_steps_exceeded"
        )
        if task_state is not None:
            task_state.update(status="FAILED", phase="finished", stop_reason="max_steps_exceeded")
        return _format_agent_result(
            "max_steps_exceeded",
            "工具调用次数过多，已终止",
            trace,
            task_id,
            return_task_id
        )

    except InterruptedError:
        # guarded_call_model 在模型请求前后发现取消时，通过该分支统一落库。
        return finish_cancelled()

    except Exception as e:
        # 任何未被内部处理的异常都记录为任务失败，避免任务一直停留在 RUNNING。
        try:
            task_manager.finish_task(
                task_id,
                "FAILED",
                stop_reason=f"exception:{type(e).__name__}: {e}"
            )
        except Exception:
            # 如果数据库本身也发生异常，避免覆盖最初的 Agent 异常。
            pass

        if task_state is not None:
            task_state.update(
                status="FAILED",
                phase="finished",
                current_tool=None,
                stop_reason=f"exception:{type(e).__name__}: {e}"[:240]
            )

        return _format_agent_result(
            None,
            f"Agent执行异常：{e}",
            trace,
            task_id,
            return_task_id
        )
