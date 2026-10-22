"""Local API settings presence only; no credential values or network requests."""
import json
from pathlib import Path
from dotenv import dotenv_values
ROOT=Path(__file__).resolve().parents[1]
v=dotenv_values(ROOT/'.env')
print(json.dumps({
 'generation':{'provider':v.get('LLM_PROVIDER','api'),'endpoint':v.get('LLM_BASE_URL'),'model':v.get('LLM_MODEL_NAME'),'key_configured':bool(v.get('LLM_API_KEY')),'remote_verified':False},
 'embedding':{'mode':v.get('KD_EMBEDDING_MODE','qwen'),'endpoint':v.get('QWEN_BASE_URL'),'model':v.get('QWEN_MODEL_NAME'),'dimension':1024,'key_configured':bool(v.get('QWEN_API_KEY')),'remote_verified':False}
},ensure_ascii=False,indent=2))
