"""Import model credentials directly from a local file; never display values."""
import argparse
import json
import os
from pathlib import Path
import secrets
from dotenv import dotenv_values

parser=argparse.ArgumentParser()
parser.add_argument('--source',required=True,help='Original private .env already on this Mac')
parser.add_argument('--root',default=str(Path(__file__).resolve().parents[1]))
args=parser.parse_args()
root=Path(args.root).resolve();source=Path(args.source).expanduser().resolve()
source_values=dotenv_values(source)
for key in ['LLM_API_KEY','QWEN_API_KEY']:
    if not source_values.get(key):raise SystemExit('Missing required binding: '+key)
dest=root/'.env'
if dest.exists():raise SystemExit('Refusing to overwrite existing .env; review locally or use a new project directory')
values={
 'LLM_PROVIDER':'api','LLM_API_KEY':source_values['LLM_API_KEY'],
 'LLM_BASE_URL':source_values.get('LLM_BASE_URL') or 'https://api.deepseek.com',
 'LLM_MODEL_NAME':source_values.get('LLM_MODEL_NAME') or 'deepseek-v4-flash',
 'QWEN_API_KEY':source_values['QWEN_API_KEY'],
 'QWEN_BASE_URL':source_values.get('QWEN_BASE_URL') or 'https://dashscope.aliyuncs.com/compatible-mode/v1',
 'QWEN_MODEL_NAME':source_values.get('QWEN_MODEL_NAME') or 'text-embedding-v3',
 'QWEN_ENCODING_FORMAT':'float','KD_EMBEDDING_MODE':'qwen',
 'MILVUS_HOST':'127.0.0.1','MILVUS_PORT':'19530',
 'KD_MONGO_URI':'mongodb://127.0.0.1:27017','KD_MONGO_DATABASE':'kd_dev',
 'KD_ARCHIVE':str(root/'.local/archive'),'CKP_DATA':str(root/'.local/data'),
 'CKP_OUTPUT':str(root/'.local/output'),
 'KD_MINIO_USER':'kd_local','KD_MINIO_PASSWORD':secrets.token_urlsafe(32),
}
# Dotenv single quotes, with explicit backslash/apostrophe escaping.
def quoted(value):
    if '\n' in value or '\r' in value:raise ValueError('Multiline runtime values are not supported')
    return "'"+value.replace('\\','\\\\').replace("'","\\'")+"'"
body=''.join(key+'='+quoted(value)+'\n' for key,value in values.items())
with os.fdopen(os.open(dest,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as file:file.write(body)
print('Imported local model bindings; generated private MinIO credentials. No values printed.')
