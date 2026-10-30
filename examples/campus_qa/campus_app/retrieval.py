"""固定的中文字符二元组检索；不读取评测任务及标签。"""
import math
import re
from collections import Counter
from .data import documents

RETRIEVAL_CONFIG = dict(id='cn-bigram-tfidf-v1', k=10, context='structured-clauses-v1')


def terms(text):
    text = ''.join(re.findall(r'[\u4e00-\u9fffA-Za-z0-9]', text.lower()))
    return Counter(text[i:i+2] for i in range(max(0,len(text)-1)))


def retrieve(question, corpus=None):
    corpus = corpus if corpus is not None else documents()
    query = terms(question)
    bags = [terms(d['title']+d['content']) for d in corpus]
    df = Counter(t for bag in bags for t in bag)
    scores = [sum(min(bag[t],n)*math.log(1+len(corpus)/(1+df[t])) for t,n in query.items()) for bag in bags]
    ranked = sorted(zip(corpus,scores),key=lambda pair:(-pair[1],pair[0]['doc_id']))
    return [dict(d,retrieval_score=round(score,5)) for d,score in ranked[:RETRIEVAL_CONFIG['k']]]
