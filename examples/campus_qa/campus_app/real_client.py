"""独立校园API账本；复用预留公式，不读取旧轮次费用或记录原始会话。"""
import json
import os
import time
import urllib.request
from harness.contracts import new_id,now,nonnegative
from harness.storage import StateConflict
from scripts.usage import estimate_reservation


class AppBudgetError(RuntimeError): pass


def transport(url,key,payload,timeout):
    req=urllib.request.Request(url.rstrip('/')+'/chat/completions',
        data=json.dumps(payload,ensure_ascii=False).encode('utf-8'),
        headers={'Content-Type':'application/json','Authorization':'Bearer '+key},method='POST')
    with urllib.request.urlopen(req,timeout=timeout) as response:
        return json.load(response)


class CampusModelClient:
    def __init__(self,store,config,*,enabled=False,send=None):
        self.store,self.config,self.enabled,self.send=store,config,enabled,send or transport

    def validate_config(self,confirmed):
        if not self.enabled or not confirmed: raise ValueError('真实模式未启用或本次调用未经确认')
        required={'base_url','task_model','reflection_model','task_prices_cny_per_million',
                  'reflection_prices_cny_per_million','max_tokens','timeout_seconds','budget_cny','max_calls'}
        if required-set(self.config):raise ValueError('真实模型配置缺少：'+','.join(sorted(required-set(self.config))))
        if not os.environ.get('DASHSCOPE_API_KEY'): raise ValueError('缺少DASHSCOPE_API_KEY')
        if not self.config['base_url'].startswith('https://'): raise ValueError('真实API地址必须使用HTTPS')
        for k in ['task_model','reflection_model']:
            if not self.config.get(k): raise ValueError('模型名称缺失')
        for role in ['task','reflection']:
            prices=self.config[role+'_prices_cny_per_million']
            if len(prices)!=2 or any(nonnegative(p,'费率')<=0 for p in prices):raise ValueError('费率须完整且为正数')
        if type(self.config['max_calls']) is not int or self.config['max_calls']<1 or self.config['timeout_seconds']<=0:
            raise ValueError('调用上限或超时无效')
        if type(self.config['max_tokens']) is not int or self.config['max_tokens']<1:raise ValueError('输出token上限无效')
        nonnegative(self.config['budget_cny'],'应用预算')

    def call(self,role,messages,*,confirmed):
        self.validate_config(confirmed)
        prices=self.config[role+'_prices_cny_per_million'];limit=self.config['budget_cny']
        reserve=estimate_reservation(messages,self.config['max_tokens'],prices)
        aid=new_id('api');begin=time.monotonic()
        with self.store.transaction('campus.api.reserve') as conn:
            records=[self.store.get(conn,'app_api_calls',r[0]) for r in conn.execute("SELECT id FROM documents WHERE collection='app_api_calls'")]
            if any(r['status'] in {'sending','unknown','overrun'} for r in records):raise AppBudgetError('有未决发送、未知费用或预留超限，停止新增调用，请先人工核查')
            if len(records)>=self.config['max_calls']:raise AppBudgetError('应用最大调用次数已到')
            if sum(r['accounted_cny'] for r in records)+reserve>limit+1e-12:raise AppBudgetError('应用预算不足，未发送请求')
            row=dict(attempt_id=aid,role=role,model=self.config[role+'_model'],status='sending',
                reserved_cny=reserve,accounted_cny=reserve,usage=None,created_at=now(),seconds=None,
                cost_kind='发送前预留，非账单',messages_retained=False)
            self.store.put(conn,'app_api_calls',aid,row,insert=True)
            self.store.event(conn,'campus_api_reserved',dict(attempt_id=aid,reserved_cny=reserve))
        payload=dict(model=self.config[role+'_model'],messages=messages,max_tokens=self.config['max_tokens'],
            temperature=0 if role=='task' else 1,enable_thinking=False,response_format={'type':'json_object'})
        try:
            response=self.send(self.config['base_url'],os.environ['DASHSCOPE_API_KEY'],payload,self.config['timeout_seconds'])
            usage=response.get('usage')
            if not isinstance(usage,dict) or any(type(usage.get(k)) is not int or usage[k]<0 for k in ['prompt_tokens','completion_tokens']):
                raise ValueError('响应缺少可核对的实际用量')
            cost=(usage['prompt_tokens']*prices[0]+usage['completion_tokens']*prices[1])/1e6
            row.update(status='overrun' if cost>reserve+1e-12 else 'settled',accounted_cny=cost,usage=usage,cost_kind='返回token用量估算，非账单',
                       seconds=time.monotonic()-begin)
            with self.store.transaction('campus.api.settle') as conn:
                self.store.put(conn,'app_api_calls',aid,row)
                self.store.event(conn,'campus_api_settled',dict(attempt_id=aid,accounted_cny=cost))
            if cost>reserve+1e-12:raise AppBudgetError('实际费用超过预留；本次已记账，停止后续操作并核查费率')
            choice=response['choices'][0]
            if choice.get('finish_reason')!='stop':raise ValueError('模型输出不完整，不能用于候选接受')
            return choice['message']['content'],dict(attempt_id=aid,cost_cny=cost,seconds=row['seconds'],api_calls=1)
        except Exception as exc:
            # 未完成用量结算时保留上界，超时不自动重试；不在账本保存秘密或会话正文。
            if row['status']=='sending':
                row.update(status='unknown',seconds=time.monotonic()-begin,error=type(exc).__name__,cost_kind='发送状态未知，保留预留上界')
                with self.store.transaction('campus.api.unknown') as conn:
                    self.store.put(conn,'app_api_calls',aid,row)
                    self.store.event(conn,'campus_api_unknown',dict(attempt_id=aid,error=type(exc).__name__))
            raise
