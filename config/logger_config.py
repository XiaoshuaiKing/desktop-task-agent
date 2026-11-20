import logging
from config.settings import LOG_DIR
LOG_DIR.mkdir(parents=True,exist_ok=True)
root=logging.getLogger()
if not any(getattr(h,'_agent_project_handler',False) for h in root.handlers):
    handler=logging.FileHandler(LOG_DIR/'agent.log',encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s | %(levelname)s | %(name)s | %(message)s'))
    handler._agent_project_handler=True
    root.addHandler(handler)
root.setLevel(logging.INFO)
