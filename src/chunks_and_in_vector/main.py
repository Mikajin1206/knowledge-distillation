"""Explicit sandbox commands; never scan all docs or drop existing collections."""
import argparse
import json
from .pipeline import SandboxRAG
from ckp.knowledge_types import STAGES


def main():
    parser = argparse.ArgumentParser(description='隔离开发 RAG（不提供正式发布）')
    parser.add_argument('--embedding', choices=['fixture', 'local', 'qwen'], default=None)
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('ingest').add_argument('path')
    search = sub.add_parser('search', help='跨阶段检索；可按文档类型与阶段筛选'); search.add_argument('query')
    search.add_argument('--knowledge-type', help='按文档类型属性精确筛选')
    search.add_argument('--stage', choices=STAGES, help='按项目生命周期阶段筛选')
    search.add_argument('--limit', type=int, default=5)
    for action in ('read', 'withdraw'):
        command = sub.add_parser(action, help=f'按项目阶段{action}一条知识')
        command.add_argument('stage', choices=STAGES); command.add_argument('knowledge_id')
        if action == 'read': command.add_argument('--revision', type=int)
    args = parser.parse_args()
    service = SandboxRAG(mode=args.embedding)
    try:
        if args.action == 'ingest': result = service.ingest(args.path)
        elif args.action == 'search':
            result = service.search(args.query, args.knowledge_type, args.limit, stage=args.stage)
        elif args.action == 'read': result = service.read(args.stage, args.knowledge_id, args.revision)
        else: result = service.withdraw(args.stage, args.knowledge_id)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    finally:
        service.close()

if __name__ == '__main__': main()
