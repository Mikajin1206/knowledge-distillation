"""Check the running local HTTP API without calling a remote model."""
import json
import urllib.request
import urllib.error
BASE='http://127.0.0.1:8001'
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
def request(path, data=None):
    raw=None if data is None else json.dumps(data).encode()
    req=urllib.request.Request(BASE+path,data=raw,headers={'Content-Type':'application/json'})
    with opener.open(req, timeout=30) as r:
        assert r.status==200
        return json.load(r)
assert request('/health')['status']=='ok'
assert '/rag' in request('/openapi.json')['paths']
result=request('/rag',{'action':'search','query':'采购审批','knowledge_type':'方案设计','limit':5})
assert result['results']
hit=result['results'][0]
assert hit['sandbox'] and hit['embedding_mode']=='fixture'
meta=hit['metadata']
record=request('/rag',{'action':'read','knowledge_type':hit['knowledge_type'],'knowledge_id':meta['knowledge_id']})
assert record['document']['revision']==meta['revision']
try:
    request('/rag',{'action':'invalid'})
    raise AssertionError('400 expected')
except urllib.error.HTTPError as exc:
    assert exc.code==400
print('PASS: HTTP health, OpenAPI, RAG search/read with citations, invalid action -> 400')
