import os
import botpy
import asyncio
import logging
import config.logger_config
from core.session_manager import user_locks,init_db,save_session,load_session,delete_session,DB_PATH
from core.task_manager import TaskManager
from core.task_state import TaskState
from core.agent import run_agent
from dotenv import load_dotenv
from botpy.message import C2CMessage
from tools.tools import set_runtime_context
from interfaces.command_handler import handle_cancel_command

logger=logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, "..", ".env"))

QQ_ID = os.getenv("QQ_APP_ID")
QQ_SECRET = os.getenv("QQ_APP_SECRET")

# 单次 Agent 任务限制，与 run_agent 的默认配置保持一致。
AGENT_MAX_STEPS = 25
AGENT_TIMEOUT = 250



class AgentBot(botpy.Client):
    async def on_ready(self):
        init_db()
        # 每个用户最多维护一个正在执行的任务；TaskState 中的 Event 由 Bot 和 Agent 共享。
        if not hasattr(self, "running_tasks"):
            self.running_tasks = {}
        print("机器人已启动")

    def get_user_lock(self,user_id):
        """获取或创建用户专属锁（线程安全，因为异步单线程无竞争）"""
        if user_id not in user_locks:
            user_locks[user_id] = asyncio.Lock()
        return user_locks[user_id]

    async def handle_command(self, content, user_id, message, role,messages):
        if content == 'clear':
            delete_session(user_id)
            await self.api.post_c2c_message(
                openid=user_id,
                msg_type=0,
                msg_seq=message.msg_seq,
                content="记忆已清空"
            )
            return True
        elif content == 'status':
            user_turns = sum(
                1 for msg in messages
                if msg.get("role") == "user"
            )
            result = f"当前用户：{user_id}\n 当前角色:{role}\n 当前历史消息数:{user_turns}"
            await self.api.post_c2c_message(
                openid=user_id,
                msg_type=0,
                msg_seq=message.msg_seq,
                content=result
            )
            return True
        elif content == 'help':
            result = "clear  清除记忆\n status 查看状态\n help   查看命令\n 取消/停止 取消当前任务"
            await self.api.post_c2c_message(
                openid=user_id,
                msg_type=0,
                msg_seq=message.msg_seq,
                content=result
            )
            return True
        elif content == '切换模型':
            user = load_session(user_id)
            if user["model"] == "local":
                user["model"] = "api"
                save_session(user_id,user)
                result = "已切换到API模型"
            else:
                user["model"] = "local"
                save_session(user_id,user)
                result = "已切换到本地模型"
            await self.api.post_c2c_message(
                openid=user_id,
                msg_type=0,
                msg_seq=message.msg_seq,
                content=result
            )
            return True
        return False

    async def on_c2c_message_create(self, message: C2CMessage):
        user_id = message.author.user_openid
        content = (message.content or "").strip()

        if not content:
            return

        # 关键：取消命令必须在获取用户锁之前处理。
        # 正常任务会持有该锁直到 Agent 执行结束；若在锁内处理取消，取消消息只能排队。
        handled, cancel_reply = handle_cancel_command(
            content,
            user_id,
            getattr(self, "running_tasks", {})
        )
        if handled:
            await self.api.post_c2c_message(
                openid=user_id,
                msg_type=0,
                msg_seq=message.msg_seq,
                content=cancel_reply
            )
            return

        lock = self.get_user_lock(user_id)
        async with lock:                        # 异步锁,一次只有一个人进入
            if not content:
                return
            role = "user"
            logger.info(f'用户输入:{content}')
            session = load_session(user_id)
            messages = session["content"]
            handled = await self.handle_command(
                content,
                user_id,
                message,
                role,
                messages
            )
            if handled:
                return
            messages.append({
                "role": "user",
                "content": content
            })
            save_session(user_id,session)
            set_runtime_context(
                api=self.api,
                loop=asyncio.get_running_loop(),
                user_id=user_id
            )

            model_type = load_session(user_id)["model"]
            logger.info(
                "Agent运行配置：max_steps=%d，timeout=%d秒",
                AGENT_MAX_STEPS,
                AGENT_TIMEOUT
            )
            # 创建运行时任务状态并注册到用户映射表，取消命令通过它找到当前 Event。
            task_state = TaskState(user_id, content)
            self.running_tasks[user_id] = task_state
            try:
                func_id, result, trace, task_id = await asyncio.to_thread(
                    run_agent,
                    model_type,
                    messages,
                    user_id,
                    role,
                    max_steps=AGENT_MAX_STEPS,
                    timeout=AGENT_TIMEOUT,
                    return_task_id=True,
                    task_state=task_state
                )
            finally:
                # Agent 线程返回后立即注销，避免后续取消命令误作用于已结束任务。
                if self.running_tasks.get(user_id) is task_state:
                    self.running_tasks.pop(user_id, None)

            logger.info(f"模型历程：{trace}")
            if func_id:
                result = "工具调用次数过多，已终止"
            else:
                messages.append({
                "role": "assistant",
                "content": result
            })

            save_session(user_id,session)

            # 任务状态与回复投递状态独立管理。
            # 即使任务 FAILED/TIMED_OUT，也仍然可以成功向用户发送解释性回复。
            task_manager = TaskManager(DB_PATH)

            try:
                task_manager.update_reply_status(task_id, "SENDING")
            except Exception:
                # 数据库状态更新失败不应阻止实际回复发送。
                logger.exception(
                    "更新回复状态为 SENDING 失败，task_id=%s",
                    task_id
                )

            try:
                # 只有 QQ API 正常返回，才记录为 SENT。
                await self.api.post_c2c_message(
                    openid=user_id,
                    msg_type=0,
                    msg_seq=message.msg_seq,
                    content=result
                )
            except (TimeoutError, ConnectionError, OSError) as e:
                # 网络超时/连接中断时，无法确认 QQ 平台是否已经接收消息。
                try:
                    task_manager.update_reply_status(
                        task_id,
                        "UNKNOWN",
                        error=f"{type(e).__name__}: {e}"
                    )
                except Exception:
                    logger.exception(
                        "更新回复状态为 UNKNOWN 失败，task_id=%s",
                        task_id
                    )
                logger.exception(
                    "QQ 回复发送结果不确定，task_id=%s",
                    task_id
                )
            except Exception as e:
                # 其他明确异常记录为 FAILED，不改变任务本身的 status。
                try:
                    task_manager.update_reply_status(
                        task_id,
                        "FAILED",
                        error=f"{type(e).__name__}: {e}"
                    )
                except Exception:
                    logger.exception(
                        "更新回复状态为 FAILED 失败，task_id=%s",
                        task_id
                    )
                logger.exception(
                    "QQ 回复发送失败，task_id=%s",
                    task_id
                )
            else:
                try:
                    task_manager.update_reply_status(task_id, "SENT")
                except Exception:
                    # QQ 已返回成功但数据库更新失败时，日志保留真实发送结果。
                    logger.exception(
                        "QQ 回复已发送，但数据库更新 SENT 失败，task_id=%s",
                        task_id
                    )
                else:
                    logger.info(
                        "QQ 回复发送成功，task_id=%s",
                        task_id
                    )


def run_bot():
    if not QQ_ID or not QQ_SECRET:
        raise ValueError("请先在项目.env中配置QQ_APP_ID与QQ_APP_SECRET")
    intents = botpy.Intents.all()
    bot = AgentBot(intents=intents)

    bot.run(
        appid=QQ_ID,
        secret=QQ_SECRET
    )
