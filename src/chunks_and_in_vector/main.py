"""Explicit sandbox commands; never scan all docs or drop existing collections."""
import argparse
import json
from .pipeline import SandboxRAG


def main():
    parser = argparse.ArgumentParser(description='隔离开发 RAG（不提供正式发布）')
    parser.add_argument('--embedding', choices=['fixture', 'local', 'qwen'], default=None)
    sub = parser.add_subparsers(dest='action', required=True)
    sub.add_parser('ingest').add_argument('path')
    search = sub.add_parser('search'); search.add_argument('query')
    search.add_argument('--knowledge-type'); search.add_argument('--limit', type=int, default=5)
    for action in ('read', 'withdraw'):
        command = sub.add_parser(action)
        command.add_argument('knowledge_type'); command.add_argument('knowledge_id')
        if action == 'read': command.add_argument('--revision', type=int)
    args = parser.parse_args()
    service = SandboxRAG(mode=args.embedding)
    try:
        if args.action == 'ingest': result = service.ingest(args.path)
        elif args.action == 'search': result = service.search(args.query, args.knowledge_type, args.limit)
        elif args.action == 'read': result = service.read(args.knowledge_type, args.knowledge_id, args.revision)
        else: result = service.withdraw(args.knowledge_type, args.knowledge_id)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    finally:
        service.close()

if __name__ == '__main__': main()
