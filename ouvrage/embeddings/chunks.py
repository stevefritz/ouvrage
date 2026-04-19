"""Paragraph-level message chunking for fine-grained semantic search."""

import re

MIN_CHUNK_LENGTH = 500


def chunk_message(content: str) -> list[dict] | None:
    """Split markdown content into sections by ## / ### headers.

    Returns None if content is too short, has no markdown headers,
    or splitting produces only one section.
    """
    if len(content) < MIN_CHUNK_LENGTH:
        return None
    if not re.search(r'^#{1,3} ', content, re.MULTILINE):
        return None
    sections = re.split(r'(?=^#{1,3} )', content, flags=re.MULTILINE)
    chunks = []
    idx = 0
    for section in sections:
        section = section.strip()
        if not section:
            continue
        heading_match = re.match(r'^#{1,3}\s+(.+?)$', section, re.MULTILINE)
        heading = heading_match.group(1) if heading_match else None
        chunks.append({"chunk_index": idx, "heading": heading, "content": section})
        idx += 1
    return chunks if len(chunks) > 1 else None
