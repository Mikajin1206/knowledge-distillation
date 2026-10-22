"""Explicit one-time download of CPU BGE weights; no model-provider API key."""
from pathlib import Path
import hashlib
import json
import sys
ROOT=Path(__file__).resolve().parents[1]
policy=json.loads(Path('/etc/codex/network-policy.json').read_text())
http=policy.get('http_network_policy',{})
allowed={rule['host'] for rule in http.get('egress_rules',[])}
if http.get('type')!='unrestricted' and 'huggingface.co' not in allowed:
    print('BLOCKED: cloud HTTP policy does not allow huggingface.co. Allow the official model host and any required download redirect hosts, or provide the model directory offline.')
    sys.exit(2)
from huggingface_hub import snapshot_download
folder=ROOT/'.cloud/models/bge-small-zh-v1.5'
snapshot_download(repo_id='Qdrant/bge-small-zh-v1.5',local_dir=folder,
                  allow_patterns=['model_optimized.onnx','tokenizer.json','config.json',
                                  'tokenizer_config.json','special_tokens_map.json','README.md'])
manifest={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in folder.iterdir() if p.is_file()}
(folder/'download-manifest.json').write_text(json.dumps(manifest,indent=2))
sys.path.insert(0,str(ROOT/'src'))
from chunks_and_in_vector.local_embedding import LocalBGEEmbedding
embedding=LocalBGEEmbedding(folder)
assert len(embedding.embed_query('采购审批'))==512
print('PASS: local BGE CPU model installed, 512-dimensional query embedding produced')
