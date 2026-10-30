"""本机应用接口；所有写操作走后端与SQLite，不暴露标签或凭据。"""
from pathlib import Path
from typing import Literal
from fastapi import FastAPI,Request
from fastapi.responses import FileResponse,JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel,ConfigDict,Field
from harness.storage import StateConflict,IntegrityError
from .data import DISCLAIMER
from .service import CampusService
from .real_client import AppBudgetError


class StrictBody(BaseModel):model_config=ConfigDict(extra='forbid')
class ModeBody(StrictBody):
    mode:Literal['offline','real']='offline'
    confirm_paid:bool=False
class ChatBody(ModeBody):
    question:str=Field(min_length=1,max_length=3000)
    retain_trace:bool=False
    use_experiment:bool=False
class FeedbackBody(StrictBody):
    run_id:str
    category:Literal['回答不准确','遗漏条件','引用不匹配']
    note:str=Field(default='',max_length=500)
class ProposeBody(ModeBody):fixture:Literal['improvement','regression']='improvement'
class CandidateBody(ModeBody):candidate_id:str
class RejectBody(StrictBody):
    candidate_id:str
    reason:str=Field(min_length=1,max_length=500)
class RollbackBody(StrictBody):
    version_id:str
    reason:str=Field(min_length=1,max_length=500)
class PublishBody(StrictBody):
    version_id:str
    confirm_review:bool=False


class FactResult(BaseModel):
    slot:str
    value:str
    citations:list[str]
class AnswerResult(BaseModel):
    facts:list[FactResult]
    escalate:bool
    reason:Literal['','conflict','missing_evidence']
    conflicts:list[str]
class ChatResult(BaseModel):
    answer:AnswerResult
    evidence:list[dict]
    seconds:float
    api_calls:int
    cost_cny:float
    run_id:str
    strategy_version:str
    mode:Literal['offline','real']
    disclaimer:str
    blocked_unsupported:bool
class ProposalResult(BaseModel):
    candidate:dict
    proposal:dict
class EvaluationResult(BaseModel):
    model_config=ConfigDict(extra='allow')
    candidate_id:str
    mode:str
    passed:bool
    metrics:dict
    reasons:list[str]
    verdict:str='rejected'
    gates:list[dict]=Field(default_factory=list)
class StateResult(BaseModel):
    current:dict
    components:dict[str,str]
    candidates:list[dict]
    evaluations:list[dict]
    decisions:list[dict]
    versions:list[dict]
    traces:list[dict]
    proposal_failures:list[dict]
    service:dict
    holdout:dict
    budget:dict
    failed_runs:list[dict]
    real_enabled:bool
    disclaimer:str
    evaluator:str
    dataset_counts:dict[str,int]
    events:list[dict]


def create_app(state_dir=None,*,allow_real=False,config=None,send=None):
    service=CampusService(state_dir or Path(__file__).resolve().parents[1]/'state/campus_app',
                          allow_real=allow_real,config=config,send=send)
    app=FastAPI(title='CampusEvolve 校园智服',version='0.1.0',docs_url='/api/docs',redoc_url=None)
    app.state.campus=service
    app.add_middleware(TrustedHostMiddleware,allowed_hosts=['localhost','127.0.0.1','testserver'])

    @app.middleware('http')
    async def local_origin(request:Request,call_next):
        origin=request.headers.get('origin')
        if request.method not in {'GET','HEAD','OPTIONS'} and origin and origin not in {str(request.base_url).rstrip('/')}:
            return JSONResponse(status_code=403,content={'detail':'只允许本机同源页面提交操作'})
        response=await call_next(request)
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Content-Security-Policy']="default-src 'self'; style-src 'self'; script-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
        return response

    @app.exception_handler(ValueError)
    async def invalid(request,exc):return JSONResponse(status_code=400,content={'detail':str(exc)})
    @app.exception_handler(KeyError)
    async def missing(request,exc):return JSONResponse(status_code=404,content={'detail':'未找到对应记录'})
    @app.exception_handler(StateConflict)
    async def conflict(request,exc):return JSONResponse(status_code=409,content={'detail':str(exc)})
    @app.exception_handler(AppBudgetError)
    async def budget(request,exc):return JSONResponse(status_code=409,content={'detail':str(exc)})
    @app.exception_handler(IntegrityError)
    async def integrity(request,exc):return JSONResponse(status_code=409,content={'detail':str(exc)})
    @app.exception_handler(Exception)
    async def failure(request,exc):return JSONResponse(status_code=500,content={'detail':'操作失败，未切换为模拟成功。请检查本地运行记录。','error_type':type(exc).__name__})

    def perform(fn,*args,**kwargs):
        with service.lock:return fn(*args,**kwargs)

    @app.get('/api/health')
    def health():return dict(status='ok',application='CampusEvolve',default_mode='offline',real_enabled=allow_real,disclaimer=DISCLAIMER)
    @app.get('/api/policies')
    def policies():return dict(documents=service.corpus,disclaimer=DISCLAIMER)
    @app.post('/api/chat',response_model=ChatResult)
    def chat(body:ChatBody):return perform(service.chat,body.question,mode=body.mode,confirmed=body.confirm_paid,
        retain=body.retain_trace,use_experiment=body.use_experiment)
    @app.post('/api/feedback')
    def feedback(body:FeedbackBody):return perform(service.feedback,body.run_id,body.category,body.note)
    @app.get('/api/evolution/state',response_model=StateResult)
    def state():return perform(service.state)
    @app.post('/api/evolution/propose',response_model=ProposalResult)
    def propose(body:ProposeBody):return perform(service.propose,body.mode,body.confirm_paid,body.fixture)
    @app.post('/api/evolution/validate',response_model=EvaluationResult)
    def validate(body:CandidateBody):return perform(service.validate,body.candidate_id,body.mode,body.confirm_paid)
    @app.post('/api/evolution/accept')
    def accept(body:CandidateBody):return perform(service.accept,body.candidate_id)
    @app.post('/api/evolution/reject')
    def reject(body:RejectBody):return perform(service.reject,body.candidate_id,body.reason)
    @app.post('/api/evolution/rollback')
    def rollback(body:RollbackBody):return perform(service.rollback,body.version_id,body.reason)
    @app.post('/api/evolution/publish')
    def publish(body:PublishBody):return perform(service.publish,body.version_id,body.confirm_review)
    @app.post('/api/evolution/freeze')
    def freeze():return perform(service.freeze)
    @app.post('/api/evolution/holdout')
    def holdout(body:ModeBody):return perform(service.holdout,body.mode,body.confirm_paid)
    @app.post('/api/evolution/run-demo')
    def demo():return perform(service.run_demo)
    @app.get('/api/research')
    def research():return dict(hotpot=[dict(stage='R1独立测试',n=30,initial_em=63.33,gepa_em=76.67,gepa_f1=82.15),
        dict(stage='R2新测试',n=30,gepa_em=76.67,manual_r2_em=70.0,manual_r2_f1=80.90)],
        campus=dict(documents=10,proposal=12,validation=12,holdout=12,is_synthetic=True,
            limitation='三组问题为同类意图的措辞变体，语义独立性有限，不证明真实校园泛化或统计显著性。'),
        implemented=['固定中文检索','结构化事实引用','反思API接入（默认关闭）','持久化独立评分','门禁与原子接受','重启恢复与祖先回滚'],
        unverified=['真实模型候选质量与收益','真实高校授权资料接入','实际学生试用与部署'],
        dependencies=[dict(name='FastAPI',version='0.115.12',license='MIT',source='https://github.com/fastapi/fastapi'),
                      dict(name='Uvicorn',version='0.34.2',license='BSD-3-Clause',source='https://github.com/encode/uvicorn'),
                      dict(name='HTTPX（仅测试）',version='0.28.1',license='BSD-3-Clause',source='https://github.com/encode/httpx')])
    static=Path(__file__).with_name('static')
    app.mount('/static',StaticFiles(directory=static),name='static')
    @app.get('/')
    def home():return FileResponse(static/'index.html')
    return app
