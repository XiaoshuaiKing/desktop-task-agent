import os
import sqlite3
import json

from pathlib import Path

SYSTEM_PROMPT = {
    "role": "system",
    "content":  """
你是Windows桌面智能助手，负责根据用户需求完成任务。

规则：
1. 当任务需要使用工具时，只能选择提供的工具列表中的工具。
2. 工具调用必须按照以下JSON格式输出：
{"tool":"工具名","arguments":{}}
3. 工具名称和参数必须严格符合工具定义，不允许编造不存在的工具。
4. 如果没有可用工具能够完成用户需求，直接告诉用户当前没有对应能力。
5. 工具执行结果会返回给你，请根据结果继续完成任务或回复用户。
"""
}
user_locks = {}

# 会话裁剪的两个维度：
#   主维度是 token 预算——一轮可能 200 token 也可能 4000，按轮数裁是拍脑袋；
#   轮数只做兜底，防止极端情况下无限增长。
SESSION_MAX_TURNS = 30
SESSION_MAX_TOKENS = 8000


def trim_session(messages, max_turns=SESSION_MAX_TURNS, max_tokens=SESSION_MAX_TOKENS):
    """裁剪会话历史：先按轮数兜底，再按 token 预算压。

    压的时候复用 context_builder 的同一套逻辑：先压工具结果（保留头尾），
    再成组丢弃中间消息。成组是为了不产生孤儿 tool_call。
    任何异常都退回原样——宁可多存 token，也不能丢消息。
    """
    if len(messages) <= 1:
        return messages

    system = messages[0]
    user_indices = [i for i, msg in enumerate(messages) if msg.get("role") == "user"]

    # 1) 轮数兜底（只在极端情况触发）
    if len(user_indices) > max_turns:
        keep_from = user_indices[-max_turns]
        messages[:] = [system] + messages[keep_from:]

    # 2) token 预算（主维度）
    try:
        from core.context_builder import Budget, build_context
        trimmed, record = build_context(
            list(messages),
            goal_text="",
            budget=Budget(total=max_tokens),
        )
        if record.changed and trimmed:
            messages[:] = trimmed
    except Exception:
        pass  # 压缩失败时保留原样，绝不能因为裁剪把消息弄丢

    return messages

DB_PATH=Path(__file__).resolve().parent.parent/"data/agent.db"

def init_db(db_path=None):
    """建表并补齐缺失字段。

    db_path 仅供测试与中断检测使用；不传时使用项目库，保持原有调用方式不变。
    """
    path=Path(db_path) if db_path is not None else DB_PATH
    path.parent.mkdir(parents=True,exist_ok=True)
    conn=sqlite3.connect(path)
    cursor=conn.cursor()
    # 数据库表结构
    # user_id	TEXT	用户 ID，主键，唯一
    # model	    TEXT	使用的模型，比如 gpt-4
    # content	TEXT	会话内容，实际存的是 JSON 字符串
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS sessions(
            user_id TEXT PRIMARY KEY,
            model TEXT NOT NULL,
            content TEXT NOT NULL
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tasks(
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
    """)

    # 兼容已经存在的旧数据库：只补充缺少的回复状态字段，不重建表、不删除历史任务。
    cursor.execute("PRAGMA table_info(tasks)")
    existing_columns = {
        row[1] for row in cursor.fetchall()
    }
    migrations = [
        ("reply_status", "ALTER TABLE tasks ADD COLUMN reply_status TEXT NOT NULL DEFAULT 'NOT_SENT'"),
        ("reply_error", "ALTER TABLE tasks ADD COLUMN reply_error TEXT"),
        ("reply_sent_at", "ALTER TABLE tasks ADD COLUMN reply_sent_at TEXT"),
    ]
    for column_name, sql in migrations:
        if column_name not in existing_columns:
            cursor.execute(sql)

    # 长期记忆：pinned 是一小段结构化事实（偏好/约束），不参与上下文裁剪。
    cursor.execute("PRAGMA table_info(sessions)")
    session_columns = {row[1] for row in cursor.fetchall()}
    if "pinned" not in session_columns:
        cursor.execute("ALTER TABLE sessions ADD COLUMN pinned TEXT")

    conn.commit()
    conn.close()

def save_session(user_id,session):
    DB_PATH.parent.mkdir(parents=True,exist_ok=True)
    conn=sqlite3.connect(DB_PATH)
    cursor=conn.cursor()

    trim_session(session["content"])
    content=json.dumps(session["content"],ensure_ascii=False)

    cursor.execute("""
        INSERT INTO sessions(user_id,model,content)
        VALUES(?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET
        model=excluded.model,
        content=excluded.content
    """,(user_id,session["model"],content))

    conn.commit()
    conn.close()

def load_session(user_id):
    DB_PATH.parent.mkdir(parents=True,exist_ok=True)
    conn=sqlite3.connect(DB_PATH)
    cursor=conn.cursor()

    cursor.execute(
        "SELECT model,content FROM sessions WHERE user_id=?",
        (user_id,)
    )

    row=cursor.fetchone()
    conn.close()

    if row is None:
        session={
            "content":[SYSTEM_PROMPT.copy()],
            "model":os.getenv("AGENT_DEFAULT_MODEL","api")
        }
        save_session(user_id,session)
        return session
    
    return {
        "model":row[0],
        "content":json.loads(row[1])
    }

def delete_session(user_id):
    DB_PATH.parent.mkdir(parents=True,exist_ok=True)
    conn=sqlite3.connect(DB_PATH)
    cursor=conn.cursor()
    cursor.execute("DELETE FROM sessions WHERE user_id=?",(user_id,))
    conn.commit()
    conn.close()

