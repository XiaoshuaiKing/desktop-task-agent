from contextlib import asynccontextmanager
import threading
from fastapi import FastAPI
from pydantic import BaseModel,Field
from core.session_manager import init_db,load_session,save_session
from core.agent import run_agent

@asynccontextmanager
async def lifespan(app):
    init_db()
    yield

app=FastAPI(title='Agent Project',lifespan=lifespan)
locks={}
guard=threading.Lock()
class ChatRequest(BaseModel):
    user_id:str=Field(min_length=1,max_length=128)
    message:str=Field(min_length=1,max_length=20000)

@app.get('/health')
def health():return {'status':'ok'}

@app.post('/chat')
def chat(request:ChatRequest):
    with guard:lock=locks.setdefault(request.user_id,threading.Lock())
    with lock:
        session=load_session(request.user_id)
        messages=session['content'];messages.append({'role':'user','content':request.message})
        _,answer,trace=run_agent(session['model'],messages,request.user_id,'guest')
        messages.append({'role':'assistant','content':answer})
        save_session(request.user_id,session)
    return {'answer':answer,'trace':trace}
