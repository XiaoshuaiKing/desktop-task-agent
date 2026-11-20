"""Project entry point; --check performs no network requests or tool execution."""
import argparse
import json
from config import settings
def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--mode',choices=['api','qq'],default='api')
    parser.add_argument('--check',action='store_true')
    parser.add_argument('--port',type=int,default=8000)
    args=parser.parse_args()
    if args.check:
        from core.skill_manager import load_skills
        from tools.tool_schemas import TOOL_SCHEMAS
        from core.agent import run_agent
        from interfaces.api_server import app
        from core.recovery import mark_stale_interrupted
        # --check 只读：dry_run 只扫描不写库，保持自检无副作用。
        stale=mark_stale_interrupted(dry_run=True)
        print(json.dumps({'project':str(settings.ROOT),'tools':len(TOOL_SCHEMAS),'skills':[s['name'] for s in load_skills()],'api_import':'ok','stale_tasks':stale},ensure_ascii=False))
        return
    # 启动前先处理上次进程残留的任务：只标记为 INTERRUPTED，不重跑。
    # 重跑可能让已经生效的外部动作（发消息、发文件）再发生一次。
    from core.recovery import format_report,mark_stale_interrupted
    print(format_report(mark_stale_interrupted()))
    if args.mode=='qq':
        from interfaces.bot import run_bot
        run_bot()
    else:
        import uvicorn
        uvicorn.run('interfaces.api_server:app',host='127.0.0.1',port=args.port)
if __name__=='__main__':main()
