from dataclasses import dataclass

from app.services.parsing.base import ParsedBlock

SEPARATORS = ["\n\n", "\n", "。", "!", "?", "；", " ", ""]


@dataclass
class Chunk:
    content: str
    page_no: int | None
    char_len: int


def _hard_pieces(text: str, size: int, overlap: int) -> list[str]:
    step = max(size - overlap, 1)
    return [text[i : i + size] for i in range(0, len(text), step)]


def _split_text(text: str, size: int, overlap: int, sep_index: int = 0) -> list[str]:
    if len(text) <= size:
        return [text]
    if sep_index >= len(SEPARATORS):
        return _hard_pieces(text, size, overlap)
    sep = SEPARATORS[sep_index]
    if sep:
        parts = [p for p in text.split(sep) if p]
    else:
        return _hard_pieces(text, size, overlap)
    pieces: list[str] = []
    current = ""
    for part in parts:
        candidate = f"{current}{sep}{part}" if current else part
        if len(candidate) <= size:
            current = candidate
        else:
            if current:
                pieces.append(current)
            if len(part) <= size:
                current = part
            else:
                pieces.extend(_split_text(part, size, overlap, sep_index + 1))
                current = ""
    if current:
        pieces.append(current)

    if sep_index > 0 or overlap <= 0 or len(pieces) <= 1:
        return pieces
    merged = [pieces[0]]
    for prev, nxt in zip(pieces, pieces[1:]):
        merged.append(f"{prev[-overlap:]}{nxt}" if len(prev) > overlap else nxt)
    return merged


def split_blocks(
    blocks: list[ParsedBlock], chunk_size: int = 1000, overlap: int = 150
) -> list[Chunk]:
    chunks: list[Chunk] = []
    for block in blocks:
        content = block.content if block.is_table else block.content.strip()
        if not content.strip():
            continue
        if block.is_table or len(content) <= chunk_size:
            pieces = [content]
        else:
            pieces = _split_text(content, chunk_size, overlap)
        for piece in pieces:
            if not block.is_table:
                piece = piece.strip()
            if piece:
                chunks.append(
                    Chunk(content=piece, page_no=block.page_no, char_len=len(piece))
                )
    return chunks
