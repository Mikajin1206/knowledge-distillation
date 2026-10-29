"""Read-only adapter for the single existing knowledge MCP.

The injected authorizer must check the trusted actor and each individual source.
The local knowledge MCP itself does not enforce production resource permissions.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Awaitable, Callable, Literal, Protocol

Corpus = Literal['project', 'product']


class Session(Protocol):
    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any: ...


@dataclass(frozen=True)
class EvidenceRef:
    corpus: Corpus
    identifier: str
    revision: int
    digest: str

    def __post_init__(self) -> None:
        if (self.corpus not in ('project', 'product') or
                not isinstance(self.identifier, str) or not self.identifier.strip() or
                not isinstance(self.digest, str) or not self.digest.strip()):
            raise ValueError('A corpus, identifier and content hash are required')
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 1:
            raise ValueError('Revision must be a positive integer')


def evidence_ref(corpus: Corpus, hit: dict[str, Any]) -> EvidenceRef:
    """Parse a search hit using the exact fields emitted by the existing MCP."""
    metadata = hit.get('metadata')
    if not isinstance(metadata, dict):
        raise KnowledgeError('invalid_response')
    identity = hit if corpus == 'product' else metadata
    id_key, digest_key = (('material_id', 'source_sha256') if corpus == 'product'
                          else ('knowledge_id', 'content_hash'))
    try:
        ref = EvidenceRef(corpus, identity.get(id_key), identity.get('revision'), identity.get(digest_key))
    except (ValueError, TypeError) as exc:
        raise KnowledgeError('invalid_response') from exc
    # Conflicting duplicate identity fields must never be silently resolved.
    for key, value in ((id_key, ref.identifier), ('revision', ref.revision), (digest_key, ref.digest)):
        if key in metadata and metadata[key] != value:
            raise KnowledgeError('identity_mismatch')
    if not isinstance(metadata.get('section_id'), str) or not metadata['section_id'].strip():
        raise KnowledgeError('invalid_response')
    return ref


def normalize_evidence(corpus: Corpus, hit: dict[str, Any]) -> dict[str, Any]:
    """Map known source fields without inferring undocumented business facts."""
    ref = evidence_ref(corpus, hit)
    metadata = hit['metadata']
    text = hit.get('text')
    if not isinstance(text, str):
        raise KnowledgeError('invalid_response')
    return {'corpus': corpus, 'identifier': ref.identifier, 'revision': ref.revision,
            'digest': ref.digest, 'section_id': metadata['section_id'],
            'heading_path': metadata.get('heading_path'), 'text': text,
            'sources': metadata.get('sources'), 'products': hit.get('products'),
            'domain': hit.get('domain'), 'stage': hit.get('stage'),
            'protected_asset_refs': metadata.get('protected_asset_refs'),
            'figure_ids': metadata.get('figure_ids'), 'quality_warnings': hit.get('quality_warnings'),
            'fact_status': None, 'score': hit.get('score'), 'original': hit}


class KnowledgeError(RuntimeError):
    """MCP or contract failure. `kind` is safe to expose as a failure category."""
    def __init__(self, kind: str):
        self.kind = kind
        super().__init__(f'Knowledge MCP: {kind}')


class KnowledgeGateway:
    def __init__(self, session: Session,
                 authorize: Callable[[str, Corpus, EvidenceRef | None], Awaitable[bool]],
                 trace: Callable[[dict[str, Any]], Awaitable[None]] | None = None):
        self._session = session
        self._authorize = authorize
        self._trace = trace

    async def _emit(self, trace_id: str, actor: str, tool: str,
                    ref: EvidenceRef | None, outcome: str) -> None:
        if self._trace is not None:
            await self._trace({'trace_id': trace_id, 'actor': actor, 'tool': tool,
                               'corpus': ref.corpus if ref else None,
                               'identifier': ref.identifier if ref else None,
                               'revision': ref.revision if ref else None,
                               'digest': ref.digest if ref else None, 'outcome': outcome})

    async def _call(self, actor: str, corpus: Corpus, ref: EvidenceRef | None,
                    tool: str, arguments: dict[str, Any], trace_id: str = '') -> dict[str, Any]:
        if not isinstance(actor, str) or not actor.strip() or await self._authorize(actor, corpus, ref) is not True:
            await self._emit(trace_id, actor, tool, ref, 'denied')
            raise PermissionError('Knowledge access denied')
        try:
            result = await self._session.call_tool(tool, arguments)
        except Exception as exc:
            await self._emit(trace_id, actor, tool, ref, 'transport_error')
            raise KnowledgeError('transport_error') from exc
        if getattr(result, 'is_error', getattr(result, 'isError', False)):
            await self._emit(trace_id, actor, tool, ref, 'tool_error')
            raise KnowledgeError('tool_error')
        payload = getattr(result, 'structured_content', getattr(result, 'structuredContent', None))
        if payload is None:
            # MCPServer 2.3 serializes unconstrained dict returns as one JSON
            # text block rather than structured_content.
            content = getattr(result, 'content', None)
            if isinstance(content, list) and len(content) == 1 and getattr(content[0], 'type', None) == 'text':
                try:
                    payload = json.loads(content[0].text)
                except (ValueError, TypeError, AttributeError):
                    pass
        if not isinstance(payload, dict):
            await self._emit(trace_id, actor, tool, ref, 'invalid_response')
            raise KnowledgeError('invalid_response')
        return payload

    async def search(self, actor: str, corpus: Corpus, query: str, *, limit: int = 5,
                     trace_id: str = '') -> dict[str, Any]:
        if (corpus not in ('project', 'product') or not isinstance(query, str) or
                not query.strip() or isinstance(limit, bool) or not isinstance(limit, int) or
                not 1 <= limit <= 20):
            raise ValueError('Valid corpus, query and limit required (1–20)')
        tool = 'search_knowledge' if corpus == 'project' else 'search_product_materials'
        payload = await self._call(actor, corpus, None, tool, {'query': query, 'limit': limit}, trace_id)
        hits = payload.get('results')
        if not isinstance(hits, list):
            await self._emit(trace_id, actor, tool, None, 'invalid_response')
            raise KnowledgeError('invalid_response')
        allowed = []
        for hit in hits:
            if not isinstance(hit, dict):
                await self._emit(trace_id, actor, tool, None, 'invalid_response')
                raise KnowledgeError('invalid_response')
            try:
                ref = evidence_ref(corpus, hit)
            except KnowledgeError:
                await self._emit(trace_id, actor, tool, None, 'invalid_response')
                raise
            if await self._authorize(actor, corpus, ref) is True:
                allowed.append(hit)
                await self._emit(trace_id, actor, tool, ref, 'allowed')
            else:
                await self._emit(trace_id, actor, tool, ref, 'denied')
        # Do not return arbitrary top-level server fields that might contain
        # inaccessible hits; only documented corpus/scope and filtered results.
        await self._emit(trace_id, actor, tool, None, 'ok')
        return {key: value for key, value in payload.items() if key in ('scope', 'corpus')} | {'results': allowed}

    async def search_evidence(self, actor: str, corpus: Corpus, query: str, *, limit: int = 5,
                              trace_id: str = '') -> list[dict[str, Any]]:
        result = await self.search(actor, corpus, query, limit=limit, trace_id=trace_id)
        return [normalize_evidence(corpus, hit) for hit in result['results']]

    @staticmethod
    def _identity(ref: EvidenceRef) -> dict[str, Any]:
        return ({'knowledge_id': ref.identifier, 'revision': ref.revision, 'content_hash': ref.digest}
                if ref.corpus == 'project' else
                {'material_id': ref.identifier, 'revision': ref.revision, 'source_sha256': ref.digest})

    async def _read(self, actor: str, ref: EvidenceRef, arguments: dict[str, Any],
                    trace_id: str) -> dict[str, Any]:
        tool = 'read_knowledge' if ref.corpus == 'project' else 'read_product_material'
        identity = self._identity(ref)
        payload = await self._call(actor, ref.corpus, ref, tool, identity | arguments, trace_id)
        if any(payload.get(key) != value for key, value in identity.items()):
            await self._emit(trace_id, actor, tool, ref, 'identity_mismatch')
            raise KnowledgeError('identity_mismatch')
        return payload

    async def read_sections(self, actor: str, ref: EvidenceRef,
                            section_ids: list[str], *, trace_id: str = '') -> dict[str, Any]:
        if (not isinstance(section_ids, list) or not section_ids or
                any(not isinstance(x, str) or not x.strip() for x in section_ids)):
            raise ValueError('At least one explicit section ID is required')
        requested = list(dict.fromkeys(section_ids))
        payload = await self._read(actor, ref, {'section_ids': requested, 'full_document': False}, trace_id)
        sections = payload.get('sections')
        key = 'id' if ref.corpus == 'project' else 'section_id'
        if (not isinstance(sections, list) or
                any(not isinstance(section, dict) or not isinstance(section.get(key), str)
                    for section in sections) or
                len(sections) != len(requested) or {section[key] for section in sections} != set(requested)):
            await self._emit(trace_id, actor, 'read_sections', ref, 'invalid_response')
            raise KnowledgeError('invalid_response')
        await self._emit(trace_id, actor, 'read_sections', ref, 'ok')
        return payload

    async def read_full_document(self, actor: str, ref: EvidenceRef, *, reason: str,
                                 trace_id: str = '') -> dict[str, Any]:
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError('A reason for full-document read is required')
        result = await self._read(actor, ref, {'full_document': True}, trace_id)
        await self._emit(trace_id, actor, 'read_full_document', ref, 'ok')
        return result

    async def read_asset(self, actor: str, ref: EvidenceRef, section_id: str, asset_id: str,
                         *, trace_id: str = '') -> dict[str, Any]:
        if (ref.corpus != 'project' or not isinstance(section_id, str) or not section_id.strip() or
                not isinstance(asset_id, str) or not asset_id.strip()):
            raise ValueError('Project reference, section ID and asset ID required')
        payload = await self._call(actor, ref.corpus, ref, 'get_asset',
                                   self._identity(ref) | {'section_id': section_id, 'asset_id': asset_id}, trace_id)
        asset = payload.get('asset')
        if (not isinstance(asset, dict) or asset.get('asset_id') != asset_id or
                asset.get('knowledge_id') != ref.identifier or asset.get('revision') != ref.revision or
                payload.get('complete') is not True):
            await self._emit(trace_id, actor, 'get_asset', ref, 'identity_mismatch')
            raise KnowledgeError('identity_mismatch')
        await self._emit(trace_id, actor, 'get_asset', ref, 'ok')
        return payload

    async def read_product_image(self, actor: str, ref: EvidenceRef, figure_id: str,
                                 *, trace_id: str = '') -> dict[str, Any]:
        """Read a returned unique figure key from the exact Word revision."""
        tool = 'read_product_material_image'
        if ref.corpus != 'product' or not isinstance(figure_id, str) or not figure_id.strip():
            raise ValueError('Product reference and explicit figure ID required')
        if not isinstance(actor, str) or not actor.strip() or await self._authorize(actor, 'product', ref) is not True:
            await self._emit(trace_id, actor, tool, ref, 'denied')
            raise PermissionError('Knowledge access denied')
        try:
            result = await self._session.call_tool(tool, self._identity(ref) | {'figure_id': figure_id})
        except Exception as exc:
            await self._emit(trace_id, actor, tool, ref, 'transport_error')
            raise KnowledgeError('transport_error') from exc
        if getattr(result, 'is_error', getattr(result, 'isError', False)):
            await self._emit(trace_id, actor, tool, ref, 'tool_error')
            raise KnowledgeError('tool_error')
        blocks = getattr(result, 'content', None)
        images = [v for v in blocks or [] if getattr(v, 'type', None) == 'image']
        texts = [v for v in blocks or [] if getattr(v, 'type', None) == 'text']
        try:
            if len(blocks) != 2 or len(images) != 1 or len(texts) != 1:
                raise ValueError('Unexpected MCP image result')
            context = json.loads(texts[0].text)
            image = images[0]
            mime = getattr(image, 'mime_type', getattr(image, 'mimeType', None))
            if (not isinstance(context, dict) or
                    any(context.get(key) != value for key, value in self._identity(ref).items()) or
                    context.get('figure_id') != figure_id or context.get('hash_verified') is not True or
                    context.get('mime_type') != mime or not isinstance(image.data, str) or not image.data):
                raise ValueError('Image identity or hash mismatch')
        except (ValueError, TypeError, AttributeError) as exc:
            await self._emit(trace_id, actor, tool, ref, 'invalid_response')
            raise KnowledgeError('invalid_response') from exc
        await self._emit(trace_id, actor, tool, ref, 'ok')
        return {'context': context, 'mime_type': mime, 'data_base64': image.data}
