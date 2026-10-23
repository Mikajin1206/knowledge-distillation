import os
import unittest
from unittest.mock import MagicMock, patch

from tests._path import *
from chunks_and_in_vector.chunking import (
    CHUNKING_STRATEGY,
    DEFAULT_CHUNK_BYTES,
    configured_chunk_bytes,
    split_section,
)
from chunks_and_in_vector.embedding import EmbeddingModel


class QwenChunkingTests(unittest.TestCase):
    def test_default_policy_is_named_utf8_bound(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(DEFAULT_CHUNK_BYTES, configured_chunk_bytes())
        self.assertEqual("utf8-byte-bound", CHUNKING_STRATEGY)

    def test_split_preserves_exact_unicode_and_character_offsets(self):
        heading = "审批说明"
        content = ("中文 emoji🙂内容。下一句！\n\n" * 80) + "末尾不补换行"
        with patch.dict(os.environ, {"KD_QWEN_CHUNK_BYTES": "512"}):
            chunks = list(split_section(heading, content))

        self.assertGreater(len(chunks), 1)
        self.assertEqual(content, "".join(fragment for fragment, _ in chunks))
        for fragment, offset in chunks:
            self.assertEqual(fragment, content[offset:offset + len(fragment)])
            rendered = f"## {heading}\n\n{fragment}"
            self.assertLessEqual(len(rendered.encode("utf-8")), 512)

    def test_uses_paragraph_then_sentence_boundaries(self):
        paragraph_content = ("A" * 180 + "。\n\n") * 8
        paragraph_chunks = list(split_section("H", paragraph_content))
        self.assertGreater(len(paragraph_chunks), 1)
        self.assertTrue(all(part.endswith("\n\n") for part, _ in paragraph_chunks[:-1]))

        sentence_content = ("B" * 100 + "。") * 12
        sentence_chunks = list(split_section("H", sentence_content))
        self.assertGreater(len(sentence_chunks), 1)
        self.assertTrue(all(part.endswith("。") for part, _ in sentence_chunks[:-1]))
        self.assertEqual(paragraph_content, "".join(part for part, _ in paragraph_chunks))
        self.assertEqual(sentence_content, "".join(part for part, _ in sentence_chunks))

    def test_heading_path_counts_and_oversized_heading_errors(self):
        heading = "长标题" * 100
        with self.assertRaisesRegex(ValueError, "章节标题路径占用"):
            list(split_section(heading, "正文"))

        heading = "短标题"
        prefix_bytes = len(f"## {heading}\n\n".encode("utf-8"))
        chunk = list(split_section(heading, "内容🙂", max_bytes=512))[0][0]
        self.assertEqual(
            prefix_bytes + len(chunk.encode("utf-8")),
            len(f"## {heading}\n\n{chunk}".encode("utf-8")),
        )

    def test_config_bounds_are_validated(self):
        for value in ("511", "4097", "abc"):
            with self.subTest(value=value), patch.dict(os.environ, {"KD_QWEN_CHUNK_BYTES": value}):
                with self.assertRaisesRegex(ValueError, "KD_QWEN_CHUNK_BYTES"):
                    configured_chunk_bytes()

    def test_embedding_entry_rejects_over_bound_before_api_call(self):
        embedding = EmbeddingModel.__new__(EmbeddingModel)
        embedding.batch_size = 8
        embedding.model = "text-embedding-v3"
        embedding.encoding_format = "float"
        embedding.dimension = None
        embedding._client = MagicMock()
        with patch.dict(os.environ, {"KD_QWEN_CHUNK_BYTES": "512"}):
            with self.assertRaisesRegex(ValueError, "utf8-byte-bound"):
                embedding.embed_documents(["🙂" * 129])
        embedding._client.embeddings.create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
