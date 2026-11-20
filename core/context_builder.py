"""上下文压缩：按 token 预算重建每轮发给模型的消息。

三条不可违反的规则（也就是面试要讲的三句）：
1. 目标锚点永不丢。最初那条 user 消息属于"不可重建的信息"。
2. 先压工具结果，后丢消息。工具结果最长、密度最低、还能重取。
3. 成组丢弃，绝不制造孤儿 tool_call。assistant 的 tool_calls 与随后的
   tool 消息必须同生共死，否则模型会看到"调了工具却没结果"。

所有函数都不抛异常：压缩失败时退回原文，宁可多花 token 也不能让任务挂掉。
"""
from dataclasses import asdict, dataclass, field
from typing import Any

# 中文按字算、英文按字符数折算，先用启发式；真实偏差可用 API 返回的
# usage.prompt_tokens 校准这两个系数。
CJK_PER_TOKEN = 1.05
ASCII_PER_TOKEN = 3.2

# 最近这几条工具结果不压缩：模型刚读到的内容（如回查的原文）如果立刻被截断，
# 其中的约束会在下一轮消失。这是评审复现的缺陷。
KEEP_RECENT_TOOL_RESULTS = 1
# 仍超预算时，assistant 的 tool_call 参数也截断到这个长度。
TOOL_ARGS_LIMIT = 200


def estimate_tokens(text: str) -> int:
    if not isinstance(text, str) or not text:
        return 0
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return int(cjk * CJK_PER_TOKEN + (len(text) - cjk) / ASCII_PER_TOKEN) + 1


def estimate_messages(messages) -> int:
    total = 0
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        total += estimate_tokens(str(msg.get("content") or ""))
        for call in msg.get("tool_calls") or []:
            fn = call.get("function") or {}
            total += estimate_tokens(str(fn.get("name") or ""))
            total += estimate_tokens(str(fn.get("arguments") or ""))
        total += 4  # 角色与结构开销
    return total


@dataclass
class Budget:
    """分层预算。total 是硬上限，其余是各层配额（用于报告，不强制切分）。"""
    total: int = 12000
    system: int = 800
    goal: int = 400
    recent: int = 8000
    tool_result: int = 2400

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CompressRecord:
    """每次压缩都要留证，否则无法回答"压缩会不会让任务变差"。"""
    before_tokens: int = 0
    after_tokens: int = 0
    compressed_tools: int = 0
    dropped_messages: int = 0
    dropped_roles: list = field(default_factory=list)
    kept_goal: bool = False
    goal_downgraded: bool = False
    goal_original_chars: int = 0
    over_budget: bool = False
    reason: str = ""

    @property
    def changed(self) -> bool:
        return self.compressed_tools > 0 or self.dropped_messages > 0 or self.goal_downgraded

    def to_dict(self) -> dict:
        data = asdict(self)
        data["changed"] = self.changed
        return data


def downgrade_goal(messages, task_id, head_chars: int = 120, index=None):
    """第三级：把目标原文移出窗口，只留开头一句 + task_id + 回查指引。

    这不是"删除"：原文仍在 tasks.description 里，模型可以用
    read_task_original(task_id) 取回。没有这个工具，这一级就等于静默删除，
    所以调用方必须先确认工具存在。

    index 必须由调用方传入**当前请求**的位置。默认取最后一条 user 消息，
    绝不能取第一条——多轮会话里那会保护历史请求、丢掉当前请求。
    """
    if index is None:
        index = max((i for i, m in enumerate(messages) if m.get("role") == "user"), default=None)
    if index is None or not task_id or index >= len(messages):
        return list(messages), 0, 0

    original = str(messages[index].get("content") or "")
    head = " ".join(original[:head_chars].split())
    replacement = (
        "[任务目标已降级]\n"
        "目标开头：%s%s\n"
        "原文共 %d 字，完整内容请调用 read_task_original(task_id=\"%s\") 取回。\n"
        "注意：执行删除/覆盖/发送等不可撤销操作前，必须先回查原文确认约束。"
        % (head, "…" if len(original) > head_chars else "", len(original), task_id)
    )
    new = list(messages)
    msg = dict(new[index])
    msg["content"] = replacement
    new[index] = msg
    return new, len(original), len(replacement)


def compress_tool_content(content: str, max_chars: int) -> tuple[str, bool]:
    """保留头尾、省略中段。头尾重要：网址/参数常在开头，报错常在结尾。"""
    if not isinstance(content, str) or len(content) <= max_chars or max_chars <= 0:
        return content, False
    head = int(max_chars * 0.6)
    tail = int(max_chars * 0.3)
    kept = (content[:head]
            + "\n…（已压缩：原文 %d 字，保留头尾，需要细节可重新读取）…\n" % len(content)
            + content[-tail:])
    return kept, True


def _is_tool(msg) -> bool:
    return isinstance(msg, dict) and msg.get("role") == "tool"


def _group(messages, start):
    """从 start 起，把 (assistant 含 tool_calls + 其后的 tool 消息) 归为一组。"""
    end = start + 1
    if isinstance(messages[start], dict) and messages[start].get("tool_calls"):
        ids = {c.get("id") for c in messages[start]["tool_calls"]}
        while end < len(messages) and _is_tool(messages[end]) and messages[end].get("tool_call_id") in ids:
            end += 1
    return end


def build_context(messages, goal_text="", budget=None,
                  task_manager=None, task_id=None, goal_index=None):
    """按预算重建消息列表，返回 (新消息列表, 压缩记录)。

    goal_index 是**当前请求**在 messages 里的下标（通常是最后一条 user）。
    不传时取最后一条 user 消息——不是第一条，多轮会话里第一条是历史请求。

    task_manager/task_id 传入时，会把压缩记录落成一条 step，便于事后核对。
    """
    record = CompressRecord()
    try:
        budget = budget or Budget()
        msgs = [m for m in messages if isinstance(m, dict)]
        if not msgs:
            return list(messages), record

        if goal_index is None:
            goal_index = max((i for i, m in enumerate(msgs) if m.get("role") == "user"), default=None)

        record.before_tokens = estimate_messages(msgs)

        # 1) 先压工具结果（最近一条除外）
        tool_indices = [i for i, m in enumerate(msgs) if _is_tool(m)]
        protected = set(tool_indices[-KEEP_RECENT_TOOL_RESULTS:]) if KEEP_RECENT_TOOL_RESULTS else set()

        def compress_tools(skip):
            changed = 0
            for i, msg in enumerate(msgs):
                if not _is_tool(msg) or i in skip:
                    continue
                text = msg.get("content")
                if not isinstance(text, str):
                    continue
                new_text, hit = compress_tool_content(text, budget.tool_result)
                if hit:
                    copied = dict(msg)
                    copied["content"] = new_text
                    msgs[i] = copied
                    changed += 1
            return changed

        record.compressed_tools += compress_tools(protected)

        # 预算还超：连最近一条一起压。保护它是为了避免"刚读到就被截断"，
        # 但预算优先——真塞不下时，截断总比发不出去好。
        if estimate_messages(msgs) > budget.total:
            record.compressed_tools += compress_tools(set())

        # 2) 在预算内保留：system + 目标锚点 + 从尾部往前成组保留
        if estimate_messages(msgs) <= budget.total:
            record.after_tokens = estimate_messages(msgs)
            record.reason = "tool_compressed_only" if record.changed else "within_budget"
            record.kept_goal = goal_text in str(msgs[0].get("content", "")) or any(
                str(m.get("content", "")).strip() == goal_text.strip() for m in msgs if m.get("role") == "user")
            # 只压工具、没丢消息，也要留证——否则无从判断压缩是否影响效果
            _record(task_manager, task_id, record)
            return msgs, record

        keep = set()
        if msgs and msgs[0].get("role") == "system":
            keep.add(0)
        if goal_index is not None:
            keep.add(goal_index)
            record.kept_goal = True

        used = estimate_messages([msgs[i] for i in sorted(keep)])
        tail = []          # 从尾部收集的组
        cursor = len(msgs)
        while cursor > 0:
            # 找当前组的起点
            start = cursor - 1
            while start > 0 and _is_tool(msgs[start]):
                start -= 1
            end = cursor
            group = msgs[start:end]
            cost = estimate_messages(group)
            if used + cost > budget.total and tail:
                record.dropped_messages += len(group)
                record.dropped_roles.extend(m.get("role") for m in group)
                cursor = start
                continue
            keep.update(range(start, end))
            used += cost
            tail.append(start)
            cursor = start

        ordered_indices = [i for i in range(len(msgs)) if i in keep]
        ordered = [msgs[i] for i in ordered_indices]

        # 3) 还不够：把当前请求的原文移出窗口（第三级降级）。原文仍可用工具回查。
        if estimate_messages(ordered) > budget.total and task_id:
            position = ordered_indices.index(goal_index) if goal_index in ordered_indices else None
            ordered, orig_chars, _ = downgrade_goal(ordered, task_id, index=position)
            if orig_chars:
                record.goal_downgraded = True
                record.goal_original_chars = orig_chars

        # 4) 还超：截断 tool_call 的参数（长参数同样占预算，之前没处理）
        if estimate_messages(ordered) > budget.total:
            for i, msg in enumerate(ordered):
                for call in msg.get("tool_calls") or []:
                    fn = call.get("function") or {}
                    args = fn.get("arguments")
                    if isinstance(args, str) and len(args) > TOOL_ARGS_LIMIT:
                        fn["arguments"] = args[:TOOL_ARGS_LIMIT] + "…[参数已截断]"

        # 5) 兜底：仍然超限就标记，由调用方决定是否中止，而不是假装没超
        record.after_tokens = estimate_messages(ordered)
        record.over_budget = record.after_tokens > budget.total
        record.reason = "over_budget" if record.over_budget else "budget_exceeded"
        _record(task_manager, task_id, record)
        return ordered, record
    except Exception as exc:  # 压缩永远不能拖垮任务
        record.reason = "fallback:%s" % type(exc).__name__
        return list(messages), record


def _record(task_manager, task_id, record):
    if task_manager is None or task_id is None or not record.changed:
        return
    try:
        step_id = task_manager.add_step(
            task_id=task_id,
            step_type="context_compression",
            name="context_compression",
            arguments=record.to_dict(),
        )
        # 记录本身也是一次已完成的动作：不更新终态，账本里会一直挂着 PENDING
        task_manager.update_step(
            task_id, step_id, "SUCCEEDED",
            result="压缩记录已写入",
        )
    except Exception:
        pass
