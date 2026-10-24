from shopdesk.rag.chunking import RecursiveChunker, approx_tokens, split_sections

DOC = """Intro paragraph before any heading.

# Returns

## Time limits
You can return items within 30 days.

## International returns
Customers outside the EU pay return shipping.

```
# not a heading, inside a code fence
```

# Shipping
We ship to 30 countries.
"""


def test_sections_follow_heading_hierarchy():
    paths = [s.heading_path for s in split_sections(DOC)]
    assert paths == [
        (),
        ("Returns", "Time limits"),
        ("Returns", "International returns"),
        ("Shipping",),
    ]


def test_heading_inside_code_fence_is_body_text():
    section = split_sections(DOC)[2]
    assert "# not a heading" in section.body


def test_long_text_splits_on_paragraphs_before_sentences():
    para = "Sentence one is here. " * 30
    text = "\n\n".join([para.strip()] * 4)
    chunker = RecursiveChunker(max_tokens=200, overlap_tokens=0)
    chunks = chunker.split_text(text)
    assert all(approx_tokens(c) <= 200 for c in chunks)
    assert len(chunks) >= 4


def test_overlap_repeats_tail_of_previous_chunk():
    words = " ".join(f"w{i}" for i in range(400))
    chunks = RecursiveChunker(max_tokens=60, overlap_tokens=15).split_text(words)
    assert len(chunks) > 3
    for prev, nxt in zip(chunks, chunks[1:], strict=False):
        assert prev.split()[-1] in nxt.split()


def test_unbreakable_run_is_hard_split():
    blob = "x" * 5000
    chunks = RecursiveChunker(max_tokens=100, overlap_tokens=10).split_text(blob)
    assert "".join(chunks) == blob
    assert all(len(c) <= 400 for c in chunks)


def test_embedding_text_carries_breadcrumb():
    chunk = RecursiveChunker().split(DOC)[1]
    assert chunk.embedding_text("Help centre").startswith(
        "Help centre > Returns > Time limits\n\n"
    )


def test_chunking_is_deterministic():
    chunker = RecursiveChunker(max_tokens=40, overlap_tokens=8)
    assert chunker.split(DOC * 5) == chunker.split(DOC * 5)
