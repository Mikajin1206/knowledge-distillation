"""Current model intent/readiness; never reveal keys or call model services."""
import importlib.util
import json
from pathlib import Path
from dotenv import dotenv_values
ROOT=Path(__file__).resolve().parents[1]
values=dotenv_values(ROOT/'.env')
folder=Path(values.get('KD_LOCAL_MODEL_PATH','/nonexistent'))
policy=json.loads(Path('/etc/codex/network-policy.json').read_text())
http=policy.get('http_network_policy',{})
hosts={r['host'] for r in http.get('egress_rules',[])}
results={
 'generation':{'provider':values.get('LLM_PROVIDER'),'requested_client_model':'GPT-6 luna',
               'reasoning_effort':'medium','application_api_enabled':values.get('LLM_PROVIDER')=='api',
               'codex_client_tested':False},
 'embedding':{'provider':'local ONNX CPU','model':'BAAI/bge-small-zh-v1.5','dimension':512,
              'runtime_installed':importlib.util.find_spec('fastembed') is not None,
              'weights_installed':(folder/'model_optimized.onnx').is_file(),
              'download_host_allowed':http.get('type')=='unrestricted' or 'huggingface.co' in hosts,
              'semantic_vectors_tested':False},
 'fixture':{'database_pipeline_tested':True,'semantic_quality_claimed':False}}
print(json.dumps(results,ensure_ascii=False,indent=2))
