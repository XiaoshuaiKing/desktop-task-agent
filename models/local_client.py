"""Connect an independently served local model using the same tool protocol."""
from .llm_client import request_chat
def call_qwen(messages,timeout=None):
    return request_chat(messages,timeout,'LOCAL_LLM')
