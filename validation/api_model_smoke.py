"""Two real API calls with synthetic input. Run only in the local configured environment."""
import json
from pathlib import Path
import sys
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from dotenv import load_dotenv
load_dotenv(ROOT/'.env')
from chunks_and_in_vector.embedding import EmbeddingModel
from ai_generate_solution.agent import _load_llm
results={}
for name,call in [('qwen',lambda:EmbeddingModel(dimension=1024).embed_documents(['本地开发验证：采购审批需要负责人复核。'])),
                  ('deepseek',lambda:_load_llm().invoke('这是部署连通性检查。只回复 OK。'))]:
    try:
        result=call()
        if name=='qwen':
            assert len(result)==1 and len(result[0])==1024
            results[name]={'passed':True,'dimension':1024}
        else:
            assert result.content
            results[name]={'passed':True,'returned_model':result.response_metadata.get('model_name'),'usage':result.usage_metadata}
    except Exception as exc:
        results[name]={'passed':False,'error_type':type(exc).__name__,'http_status':getattr(exc,'status_code',None)}
print(json.dumps(results,ensure_ascii=False,indent=2))
if not all(x['passed'] for x in results.values()):sys.exit(1)
