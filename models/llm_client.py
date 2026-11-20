"""OpenAI-compatible model requests. No network calls happen during import."""
import os
import requests
from config import settings
from tools.tool_schemas import TOOL_SCHEMAS

def request_chat(messages,timeout,prefix='LLM'):
    url=os.getenv(prefix+'_BASE_URL')
    model=os.getenv(prefix+'_MODEL')
    key=os.getenv(prefix+'_API_KEY','')
    if not url or not model:raise ValueError(f'请配置{prefix}_BASE_URL与{prefix}_MODEL')
    if timeout is not None and timeout<=0:raise TimeoutError('模型请求预算已用尽')
    headers={'Content-Type':'application/json'}
    if key:headers['Authorization']='Bearer '+key
    try:
        r=requests.post(url,headers=headers,json={'model':model,'messages':messages,'tools':TOOL_SCHEMAS,'tool_choice':'auto','temperature':.1},timeout=timeout)
        r.raise_for_status()
    except requests.exceptions.Timeout as exc:raise TimeoutError('模型请求超时') from exc
    value=r.json()['choices'][0]['message']
    return {'role':'assistant','content':value.get('content'),'tool_calls':value.get('tool_calls') or []}

def call_llm(messages,timeout):return request_chat(messages,timeout,'LLM')
