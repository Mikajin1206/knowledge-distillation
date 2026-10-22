"""No hidden cloud-model fallback; local token splitting preserves all content."""
import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock
from tests._path import *
from chunks_and_in_vector.local_embedding import LocalBGEEmbedding

class CloudModelTests(unittest.TestCase):
    def test_local_weights_missing_does_not_fall_back(self):
        with tempfile.TemporaryDirectory() as path:
            with self.assertRaisesRegex(ValueError, '不会自动改用'):
                LocalBGEEmbedding(path)

    def local(self):
        from tokenizers import Tokenizer
        from tokenizers.models import WordLevel
        from tokenizers.pre_tokenizers import Whitespace
        embedding=LocalBGEEmbedding.__new__(LocalBGEEmbedding)
        embedding.tokenizer=Tokenizer(WordLevel({'[UNK]':0},unk_token='[UNK]'))
        embedding.tokenizer.pre_tokenizer=Whitespace()
        embedding.encoder=MagicMock()
        return embedding

    def test_long_sections_split_without_truncation(self):
        embedding=self.local();content='审批 条件 待确认 '*800
        fragments=list(embedding.split_section('标题',content))
        self.assertGreater(len(fragments),1)
        self.assertEqual(content,''.join(fragment for fragment,_ in fragments))
        for fragment,offset in fragments:
            self.assertEqual(fragment,content[offset:offset+len(fragment)])
            self.assertLessEqual(len(embedding.tokenizer.encode('## 标题\n\n'+fragment).ids),512)

    def test_direct_oversized_embedding_rejected(self):
        embedding=self.local()
        with self.assertRaisesRegex(ValueError,'512'):
            embedding.embed_documents(['审批 '*1000])
        embedding.encoder.embed.assert_not_called()

    def test_codex_client_never_calls_api(self):
        from ai_generate_solution.agent import _load_llm
        with patch.dict(os.environ,{'LLM_PROVIDER':'codex-client'}), patch('ai_generate_solution.agent.ChatOpenAI') as api:
            with self.assertRaisesRegex(RuntimeError,'Codex'):_load_llm()
            api.assert_not_called()

    def test_explicit_future_api_requires_own_endpoint_and_key(self):
        from ai_generate_solution.agent import _load_llm
        with patch.dict(os.environ,{'LLM_PROVIDER':'api','LLM_BASE_URL':'','LLM_API_KEY':''}):
            with self.assertRaisesRegex(RuntimeError,'不会复用'):_load_llm()

    def test_model_intent_medium_without_unsupported_temperature(self):
        from ai_generate_solution.agent import _load_llm
        with patch.dict(os.environ,{'LLM_PROVIDER':'api','LLM_BASE_URL':'https://example.invalid/v1',
                                     'LLM_API_KEY':'test-only','LLM_MODEL_NAME':'gpt-6-luna',
                                     'LLM_REASONING_EFFORT':'medium'}),patch('ai_generate_solution.agent.ChatOpenAI') as api:
            _load_llm()
            self.assertEqual('gpt-6-luna',api.call_args.kwargs['model'])
            self.assertEqual('medium',api.call_args.kwargs['reasoning_effort'])
            self.assertNotIn('temperature',api.call_args.kwargs)

if __name__=='__main__':unittest.main()
