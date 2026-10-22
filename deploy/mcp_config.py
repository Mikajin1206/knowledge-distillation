"""Emit absolute local MCP paths without embedding model keys."""
import json
from pathlib import Path
root=Path(__file__).resolve().parents[1]
out=root/'.local/mcp.local.json';out.parent.mkdir(mode=0o700,exist_ok=True)
config={'mcpServers':{
 'knowledge-distillation':{'command':str(root/'ckp-mcp.sh'),'env':{'CKP_DATA':str(root/'.local/data'),'CKP_OUTPUT':str(root/'.local/output')}},
 'knowledge-rag-development':{'command':str(root/'rag-mcp.sh'),'env':{'KD_EMBEDDING_MODE':'qwen'}},
 'ai-generate-solution':{'command':str(root/'ai-generate-solution-mcp.sh'),'env':{'LLM_PROVIDER':'api','KD_EMBEDDING_MODE':'qwen'}}}}
out.write_text(json.dumps(config,ensure_ascii=False,indent=2)+'\n');out.chmod(0o600)
print(out)
