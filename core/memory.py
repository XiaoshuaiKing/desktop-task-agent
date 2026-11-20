"""长期记忆：结构化事实（pinned）+ 历史任务召回。

设计边界（这是本模块最值得讲的部分）：
- 只存「跨轮次仍然成立」的事实。当前任务的中间结果属于 TaskManager（state），不在这里。
- 抽取故意做得很保守：必须同时出现「规则词」（以后/默认/一直/都）和「明确取值」才写入。
  抽错一次比不抽更伤。
- 用户改口是**覆盖**，不是追加。追加会让模型看到两条矛盾规则，然后随机选一条。
- 记忆只注入 system prompt，不进入对话历史，因此不参与 trim_session 的裁剪。
"""
import json
import math
import os
import re
import sqlite3
from collections import Counter
from pathlib import Path

from core.session_manager import DB_PATH, init_db

# 只认这些显式表达；「这次放桌面」不算规则。
RULE_WORDS = ("以后", "默认", "一直", "都放", "都存", "都用", "统一")

PATH_KEYWORDS = {
    "桌面": ("Desktop",),
    "下载": ("Downloads",),
    "文档": ("Documents",),
    "图片": ("Pictures",),
}

BROWSERS = {"chrome": "chrome", "谷歌浏览器": "chrome", "edge": "edge", "微软浏览器": "edge"}

MAX_HISTORY = 3
MAX_DESC = 40

# 只在用户显式提到过去时才召回历史任务。
# 理由：会话裁剪已经是按 token 预算（约 20 轮），最近的历史本来就在 messages 里，
# 每轮无条件召回既占 token 又是噪音。触发时召回，相关性反而更高。
HISTORY_TRIGGERS = ("上次", "之前", "刚才", "上回", "那次", "一样", "同样",
                    "再来", "再生成", "再做", "继续")


def wants_history(user_text):
    """用户这句话是否在引用过去。"""
    if not isinstance(user_text, str):
        return False
    return any(word in user_text for word in HISTORY_TRIGGERS)


# ---------- 存储 ----------

def _connect(db_path=None):
    return sqlite3.connect(str(db_path or DB_PATH))


def load_pinned(user_id, db_path=None):
    """读取用户的长期事实；表或列不存在时返回空 dict，不抛异常。"""
    try:
        conn = _connect(db_path)
        try:
            row = conn.execute(
                "SELECT pinned FROM sessions WHERE user_id=?", (str(user_id),)
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.OperationalError:
        init_db(db_path)
        return {}
    if not row or not row[0]:
        return {}
    try:
        facts = json.loads(row[0])
    except (TypeError, ValueError):
        return {}
    return facts if isinstance(facts, dict) else {}


def save_pinned(user_id, facts, db_path=None):
    """整体覆盖写入；调用方负责保证 facts 已经是合并后的结果。"""
    payload = json.dumps(facts, ensure_ascii=False, sort_keys=True)
    conn = _connect(db_path)
    try:
        conn.execute(
            "INSERT INTO sessions(user_id, model, content, pinned) VALUES(?,?,?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET pinned=excluded.pinned",
            (str(user_id), os.getenv("AGENT_DEFAULT_MODEL", "api"), "[]", payload),
        )
        conn.commit()
    finally:
        conn.close()


# ---------- 抽取 ----------

def _has_rule_word(text):
    return any(word in text for word in RULE_WORDS)


def _extract_dir(text):
    for match in re.finditer(r"[A-Za-z]:\\[^\s，。；、\"'）)]+", text):
        return match.group(0).rstrip("\\")
    for keyword, parts in PATH_KEYWORDS.items():
        if keyword in text:
            home = Path(os.path.expanduser("~"))
            return str(home.joinpath(*parts))
    return None


def _extract_browser(text):
    lowered = text.lower()
    for name, value in BROWSERS.items():
        if name in lowered:
            return value
    return None


def extract_pinned(user_text, current=None):
    """从一句用户输入里抽取长期事实；返回合并后的新 dict（不修改入参）。"""
    facts = dict(current or {})
    if not isinstance(user_text, str) or not user_text.strip():
        return facts
    if not _has_rule_word(user_text):
        return facts

    target = _extract_dir(user_text)
    if target:
        facts["output_dir"] = target

    browser = _extract_browser(user_text)
    if browser:
        facts["browser"] = browser

    return facts


# ---------- 历史任务召回 ----------

def _bigrams(text):
    s = "".join(ch for ch in str(text) if ch.strip())
    return [s[i:i + 2] for i in range(len(s) - 1)]


def recall_history(user_id, query, k=MAX_HISTORY, db_path=None):
    """按字符 bigram 检索用户历史任务描述；语料只有几十条，不值得上向量。"""
    if not query or not query.strip():
        return []
    try:
        conn = _connect(db_path)
        try:
            rows = conn.execute(
                "SELECT description, status, created_at FROM tasks "
                "WHERE user_id=? ORDER BY created_at DESC LIMIT 200",
                (str(user_id),),
            ).fetchall()
        finally:
            conn.close()
    except sqlite3.OperationalError:
        return []
    if not rows:
        return []

    docs = [_bigrams(r[0]) for r in rows]
    df = Counter()
    for grams in docs:
        for gram in set(grams):
            df[gram] += 1
    n = len(docs)

    q = set(_bigrams(query))
    scored = []
    for row, grams in zip(rows, docs):
        tf = Counter(grams)
        score = 0.0
        for gram in q:
            if tf.get(gram):
                score += math.log(1 + (n - df[gram] + 0.5) / (df[gram] + 0.5))
        if score > 0:
            scored.append((score, row))
    scored.sort(key=lambda item: -item[0])
    return [{"description": r[0], "status": r[1], "created_at": r[2]}
            for _, r in scored[:k]]


# ---------- 注入 ----------

def render_block(facts, history=None):
    """拼成一小段塞进 system prompt；没有内容时返回空串。"""
    lines = []
    if facts:
        lines.append("已知用户长期约束（跨轮次有效，用户改口会以最新为准）：")
        if facts.get("output_dir"):
            lines.append("- 生成的文件默认目录：%s" % facts["output_dir"])
        if facts.get("browser"):
            lines.append("- 默认浏览器：%s" % facts["browser"])
        for key, value in sorted(facts.items()):
            if key in ("output_dir", "browser"):
                continue
            lines.append("- %s：%s" % (key, value))

    if history:
        lines.append("")
        lines.append("可能相关的历史任务（仅供参考，不要照搬参数）：")
        for item in history:
            desc = str(item["description"]).replace("\n", " ")[:MAX_DESC]
            lines.append("- [%s] %s" % (item.get("status", "?"), desc))

    return "\n".join(lines).strip()


def build_memory_block(user_id, user_text, db_path=None):
    """供 run_agent 调用：更新并返回记忆块。任何异常都返回空串，不能拖垮任务。"""
    try:
        current = load_pinned(user_id, db_path=db_path)
        updated = extract_pinned(user_text, current)
        if updated != current:
            save_pinned(user_id, updated, db_path=db_path)
        # 按需召回：没提"上次/之前/一样"就不查历史，省 token 也少噪音
        history = recall_history(user_id, user_text, db_path=db_path) if wants_history(user_text) else []
        return render_block(updated, history)
    except Exception:
        return ""
