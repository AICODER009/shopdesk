"""Markdown-aware recursive chunking.

Documents are first cut into sections at headings so a chunk never straddles two topics.
Each section is then split recursively: paragraphs, then lines, then sentences, then words,
descending only as far as needed to fit the budget. Pieces are greedily merged back up to
`max_tokens` with a token overlap between neighbours.
"""

import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

DEFAULT_SEPARATORS: tuple[str, ...] = ("\n\n", "\n", ". ", "? ", "! ", "; ", ", ", " ")

HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
FENCE = re.compile(r"^\s*(```|~~~)")


def approx_tokens(text: str) -> int:
    # ~4 chars/token holds well enough for English help-centre text. Swap in a real tokenizer
    # if the KB is mostly non-Latin.
    return math.ceil(len(text) / 4)


@dataclass(frozen=True, slots=True)
class Section:
    heading_path: tuple[str, ...]
    body: str


@dataclass(frozen=True, slots=True)
class Chunk:
    index: int
    text: str
    heading_path: tuple[str, ...]

    @property
    def breadcrumb(self) -> str:
        return " > ".join(self.heading_path)

    def embedding_text(self, title: str) -> str:
        # Headings carry a lot of the meaning ("Returns > International"); keep them in the vector.
        path = " > ".join(p for p in (title, *self.heading_path) if p)
        return f"{path}\n\n{self.text}" if path else self.text


def split_sections(markdown: str) -> list[Section]:
    sections: list[Section] = []
    stack: list[tuple[int, str]] = []
    buf: list[str] = []
    in_fence = False

    def flush() -> None:
        body = "\n".join(buf).strip()
        if body:
            sections.append(Section(tuple(title for _, title in stack), body))
        buf.clear()

    for line in markdown.splitlines():
        if FENCE.match(line):
            in_fence = not in_fence
        match = None if in_fence else HEADING.match(line)
        if match:
            flush()
            level = len(match.group(1))
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, match.group(2).strip()))
        else:
            buf.append(line)
    flush()
    return sections


class RecursiveChunker:
    def __init__(
        self,
        max_tokens: int = 350,
        overlap_tokens: int = 50,
        separators: Sequence[str] = DEFAULT_SEPARATORS,
        length_fn: Callable[[str], int] = approx_tokens,
    ) -> None:
        if overlap_tokens >= max_tokens:
            raise ValueError("overlap must be smaller than max_tokens")
        self.max_tokens = max_tokens
        self.overlap_tokens = overlap_tokens
        self.separators = tuple(separators)
        self._len = length_fn

    def split(self, markdown: str) -> list[Chunk]:
        chunks: list[Chunk] = []
        for section in split_sections(markdown):
            for text in self.split_text(section.body):
                chunks.append(Chunk(len(chunks), text, section.heading_path))
        return chunks

    def split_text(self, text: str) -> list[str]:
        return [c for c in (p.strip() for p in self._split(text, self.separators)) if c]

    def _split(self, text: str, separators: tuple[str, ...]) -> list[str]:
        if self._len(text) <= self.max_tokens:
            return [text]

        for i, sep in enumerate(separators):
            if sep in text:
                remaining = separators[i + 1 :]
                break
        else:
            return self._hard_split(text)

        pieces: list[str] = []
        parts = text.split(sep)
        for j, part in enumerate(parts):
            # Keep the separator attached so merged chunks read naturally.
            piece = part + sep if j < len(parts) - 1 else part
            if not piece.strip():
                continue
            if self._len(piece) <= self.max_tokens:
                pieces.append(piece)
            else:
                pieces.extend(self._split(piece, remaining))
        return self._merge(pieces)

    def _merge(self, pieces: list[str]) -> list[str]:
        out: list[str] = []
        window: list[str] = []
        size = 0
        for piece in pieces:
            n = self._len(piece)
            if window and size + n > self.max_tokens:
                out.append("".join(window))
                while window and (size > self.overlap_tokens or size + n > self.max_tokens):
                    size -= self._len(window.pop(0))
            window.append(piece)
            size += n
        if window:
            out.append("".join(window))
        return out

    def _hard_split(self, text: str) -> list[str]:
        # Last resort for a single unbroken token run (URLs, base64 blobs).
        step = self.max_tokens * 4
        return [text[i : i + step] for i in range(0, len(text), step)]
