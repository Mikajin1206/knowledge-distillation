"""CPU-only BGE Chinese embeddings from explicitly installed local ONNX weights."""
import os
from pathlib import Path

MODEL = 'BAAI/bge-small-zh-v1.5'

class LocalBGEEmbedding:
    dimension = 512
    model = MODEL

    def __init__(self, model_path=None):
        path = Path(model_path or os.getenv('KD_LOCAL_MODEL_PATH', '')).expanduser()
        required = ['model_optimized.onnx', 'tokenizer.json', 'config.json',
                    'tokenizer_config.json', 'special_tokens_map.json']
        if not path.is_dir() or any(not (path/name).is_file() for name in required):
            raise ValueError('本机 BGE 权重未安装；KD_LOCAL_MODEL_PATH 必须指向 Qdrant/bge-small-zh-v1.5 ONNX 模型目录。不会自动改用 Qwen 或测试向量。')
        from fastembed import TextEmbedding
        from tokenizers import Tokenizer
        self.encoder = TextEmbedding(MODEL, specific_model_path=str(path.resolve()),
                                     local_files_only=True, threads=2,
                                     providers=['CPUExecutionProvider'])
        self.tokenizer = Tokenizer.from_file(str(path/'tokenizer.json'))
        self.tokenizer.no_truncation(); self.tokenizer.no_padding()

    def split_section(self, heading, content):
        prefix = f'## {heading}\n\n'
        budget = 510 - len(self.tokenizer.encode(prefix, add_special_tokens=False).ids)
        if budget < 32:
            raise ValueError('章节标题过长，无法在 BGE 512-token 窗口内保留正文')
        offsets = self.tokenizer.encode(content, add_special_tokens=False).offsets
        cursor = 0
        for i in range(0, len(offsets), budget):
            end = offsets[min(i+budget, len(offsets))-1][1]
            if i+budget >= len(offsets): end = len(content)
            yield content[cursor:end], cursor
            cursor = end

    def embed_documents(self, texts):
        # Never silently truncate when invoked outside the ingestion splitter.
        for text in texts:
            if len(self.tokenizer.encode(text).ids) > 512:
                raise ValueError('BGE 输入超过 512 tokens，必须先按 tokenizer 分片')
        return [vector.tolist() for vector in self.encoder.embed(texts,batch_size=8)]

    def embed_query(self, text):
        if len(self.tokenizer.encode(text).ids) > 480:
            raise ValueError('查询过长，请缩短到 480 tokens 以内')
        return next(self.encoder.query_embed(text)).tolist()
