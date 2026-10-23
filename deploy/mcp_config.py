"""Emit the single local KD MCP config without embedding model keys.

CKP_LOCAL_ACTOR and CKP_REVIEWER_IDS identify this machine's single-user
development operator only; they are not server authentication credentials.
The qwen embedding mode is configured on the primary service because the
development RAG tools are exposed by that same service.
"""
import json
from pathlib import Path
root = Path(__file__).resolve().parents[1]
out = root / '.local/mcp.local.json'
out.parent.mkdir(mode=0o700, exist_ok=True)
config = {
    'mcpServers': {
        'knowledge-distillation': {
            'command': str(root / 'ckp-mcp.sh'),
            'env': {
                'CKP_DATA': str(root / '.local/data'),
                'CKP_OUTPUT': str(root / '.local/output'),
                'CKP_LOCAL_ACTOR': 'local-operator',
                'CKP_REVIEWER_IDS': 'local-operator',
                'KD_EMBEDDING_MODE': 'qwen',
            },
        },
    },
}
out.write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n')
out.chmod(0o600)
print(out)
