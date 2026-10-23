"""Conservative Qwen input sizing without relying on a model tokenizer.

The byte limit is an explicitly named UTF-8 bound, not a token count.  It is
intended to leave ample headroom for Qwen's tokenizer while preventing the
pipeline from sending arbitrarily large inputs.
"""
from __future__ import annotations

import os
import re
from typing import Iterator, Tuple


CHUNKING_STRATEGY = "utf8-byte-bound"
DEFAULT_CHUNK_BYTES = 512
MIN_CHUNK_BYTES = 512
MAX_CHUNK_BYTES = 4096
_PARAGRAPH_END = re.compile(r"\n[ \t]*\n+")
_SENTENCE_END = re.compile(r"[。！？!?；;]+[”’\"'）】》」』]*")


def configured_chunk_bytes() -> int:
    """Return the configured conservative UTF-8 byte bound."""
    raw = os.getenv("KD_QWEN_CHUNK_BYTES")
    if raw is None or raw == "":
        return DEFAULT_CHUNK_BYTES
    try:
        value = int(raw)
    except ValueError:
        raise ValueError("KD_QWEN_CHUNK_BYTES 必须是 512..4096 的整数") from None
    if not MIN_CHUNK_BYTES <= value <= MAX_CHUNK_BYTES:
        raise ValueError("KD_QWEN_CHUNK_BYTES 必须在 512..4096 之间")
    return value


def _last_match_end(pattern: re.Pattern[str], text: str, start: int, limit: int) -> int:
    last = 0
    for match in pattern.finditer(text, start, limit):
        last = match.end()
    return last


def _preferred_boundary(text: str, start: int, limit: int) -> int:
    """Choose the last paragraph boundary, then sentence boundary, if any."""
    paragraph = _last_match_end(_PARAGRAPH_END, text, start, limit)
    if paragraph:
        return paragraph
    sentence = _last_match_end(_SENTENCE_END, text, start, limit)
    return sentence or limit


def split_section(
    heading: str, content: str, max_bytes: int | None = None
) -> Iterator[Tuple[str, int]]:
    """Yield exact source fragments and character offsets for one section.

    The rendered heading path (``## {heading}\n\n``) counts toward every
    fragment's UTF-8 byte bound. Oversized paragraphs are split at sentence
    boundaries when possible, then at Unicode codepoint boundaries.
    """
    bound = configured_chunk_bytes() if max_bytes is None else max_bytes
    if not MIN_CHUNK_BYTES <= bound <= MAX_CHUNK_BYTES:
        raise ValueError("分片字节上限必须在 512..4096 之间")
    if not isinstance(heading, str) or not isinstance(content, str):
        raise TypeError("heading 和 content 必须是字符串")

    prefix = f"## {heading}\n\n"
    prefix_bytes = len(prefix.encode("utf-8"))
    if prefix_bytes > bound:
        raise ValueError(
            f"章节标题路径占用 {prefix_bytes} UTF-8 bytes，超过 {bound} bytes 上限"
        )

    if not content:
        yield "", 0
        return

    cursor = 0
    while cursor < len(content):
        used = prefix_bytes
        end = cursor
        while end < len(content):
            char_bytes = len(content[end].encode("utf-8"))
            if used + char_bytes > bound:
                break
            used += char_bytes
            end += 1

        if end == cursor:
            # A single Unicode codepoint cannot exceed the configured bound,
            # but keep the failure explicit if that assumption ever changes.
            raise ValueError("单个字符已超过 UTF-8 分片字节上限")
        if end < len(content):
            end = _preferred_boundary(content, cursor, end)
        yield content[cursor:end], cursor
        cursor = end


def validate_embedding_texts(texts: list[str], max_bytes: int | None = None) -> None:
    """Reject direct embedding inputs that exceed the same UTF-8 byte bound."""
    bound = configured_chunk_bytes() if max_bytes is None else max_bytes
    if not MIN_CHUNK_BYTES <= bound <= MAX_CHUNK_BYTES:
        raise ValueError("分片字节上限必须在 512..4096 之间")
    for index, text in enumerate(texts):
        size = len(text.encode("utf-8"))
        if size > bound:
            raise ValueError(
                f"第 {index + 1} 条向量输入为 {size} UTF-8 bytes，超过 {bound} bytes；"
                f"分片策略为 {CHUNKING_STRATEGY}，请先按章节分片"
            )
