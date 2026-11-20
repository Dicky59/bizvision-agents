from core.ingest.chunking import chunk_text, embedding_text, estimate_tokens


def test_empty_input() -> None:
    assert chunk_text("") == []
    assert chunk_text("  \n\n ") == []


def sized(label: str, tokens: int = 30) -> str:
    """A paragraph of roughly `tokens` tokens (chars / 4)."""
    return (label + " " + "x" * tokens * 4)[: tokens * 4]


def test_heading_paths_follow_hierarchy_when_sections_are_big() -> None:
    # every body is larger than the target, so nothing merges and each section keeps its own path
    big = {k: sized(k, 40) for k in ("intro", "yleista", "botti", "soita")}
    text = (
        f"{big['intro']}\n\n# Palvelut\n\n{big['yleista']}\n\n## AI-asiakaspalvelija\n\n"
        f"{big['botti']}\n\n# Yhteys\n\n{big['soita']}"
    )
    chunks = chunk_text(text, target_tokens=20, max_tokens=60)
    assert [c.heading_path for c in chunks] == [
        None,
        "Palvelut",
        "Palvelut > AI-asiakaspalvelija",
        "Yhteys",
    ]
    assert [c.content[:5] for c in chunks] == ["intro", "yleis", "botti", "soita"]
    assert [c.ordinal for c in chunks] == [0, 1, 2, 3]


def test_small_sibling_sections_merge_under_the_parent_path() -> None:
    text = "# Palvelut\n\nyleistä\n\n## Botti\n\nchat\n\n## Posti\n\nviestit"
    [chunk] = chunk_text(text)
    assert chunk.heading_path == "Palvelut"  # parent path, not the first child's
    # the parent's own heading is the heading_path; sub-headings stay as text lines
    assert chunk.content == "yleistä\n\n## Botti\n\nchat\n\n## Posti\n\nviestit"
    assert chunk.token_count == estimate_tokens(chunk.content)


def test_siblings_without_parent_body_merge_under_the_parent() -> None:
    [chunk] = chunk_text(
        "# Palvelut\n\n## Botti\n\nchat\n\n### Hinta\n\nkalliimpi\n\n## Posti\n\nx"
    )
    assert chunk.heading_path == "Palvelut"
    assert chunk.content == "## Botti\n\nchat\n\n### Hinta\n\nkalliimpi\n\n## Posti\n\nx"


def test_never_merges_across_a_higher_level_heading() -> None:
    text = "## A\n\n### a1\n\nx\n\n# B\n\ny\n\n## b1\n\nz"
    chunks = chunk_text(text)
    # '# B' is a higher-level heading than the group started at '## A': it starts a new group
    assert [(c.heading_path, c.content) for c in chunks] == [
        ("A", "## a1\n\nx".replace("## a1", "### a1")),
        ("B", "y\n\n## b1\n\nz"),
    ]


def test_unrelated_top_level_sections_are_not_merged() -> None:
    chunks = chunk_text("# Palvelut\n\nyleistä\n\n# Yhteys\n\nsoita")
    assert [(c.heading_path, c.content) for c in chunks] == [
        ("Palvelut", "yleistä"),
        ("Yhteys", "soita"),
    ]


def test_sections_under_different_parents_are_not_merged() -> None:
    text = "# P\n\n## A\n\nx\n\n# Q\n\n## B\n\ny"
    assert [(c.heading_path, c.content) for c in chunk_text(text)] == [
        ("P", "## A\n\nx"),
        ("Q", "## B\n\ny"),
    ]


def test_merging_stops_at_the_target_and_never_exceeds_the_max() -> None:
    sections = "\n\n".join(f"## S{i}\n\n{sized(f's{i}', 12)}" for i in range(12))
    chunks = chunk_text(f"# P\n\n{sections}", target_tokens=50, max_tokens=80)
    assert len(chunks) > 1
    assert {c.heading_path for c in chunks} == {"P"}
    assert all(c.token_count <= 80 for c in chunks)
    assert all(c.token_count >= 50 for c in chunks[:-1])  # grouped until the target is reached
    joined = "\n\n".join(c.content for c in chunks)
    assert all(f"## S{i}" in joined for i in range(12))  # nothing lost, order kept
    assert joined.index("## S0") < joined.index("## S11")


def test_a_section_that_would_exceed_the_max_is_not_merged() -> None:
    text = f"# P\n\n## Small\n\n{sized('small', 20)}\n\n## Big\n\n{sized('big', 70)}"
    chunks = chunk_text(text, target_tokens=50, max_tokens=80)
    # 'P' (no body) + 'Small' merge into one chunk under the parent path; 'Big' (20 + 70 tokens
    # would exceed the max of 80) stays on its own
    assert [c.heading_path for c in chunks] == ["P", "P > Big"]
    assert chunks[0].content.startswith("## Small") and "big" not in chunks[0].content
    assert all(c.token_count <= 80 for c in chunks)


def test_a_big_section_is_split_but_small_neighbours_still_merge() -> None:
    para = "\n\n".join(sized(f"p{i}", 30) for i in range(6))  # ~180 tokens > target of 50
    text = f"# P\n\n## A\n\n{sized('a', 5)}\n\n## B\n\n{para}\n\n## C\n\n{sized('c', 5)}"
    chunks = chunk_text(text, target_tokens=50, max_tokens=80)
    assert chunks[0].heading_path == "P"  # the small A section merged with the parent
    big = [c for c in chunks if c.heading_path == "P > B"]
    assert len(big) >= 3 and all(c.token_count <= 80 for c in big)
    assert any(c.content.startswith("a") or "## A" in c.content for c in chunks)
    assert sum("## C" in c.content or c.heading_path == "P > C" for c in chunks) == 1


def test_text_before_the_first_heading_joins_the_first_heading() -> None:
    [chunk] = chunk_text("// eyebrow\n\n# Title\n\nbody\n\n## Sub\n\nmore")
    assert chunk.heading_path == "Title"  # the preamble doesn't change the path
    assert chunk.content == "// eyebrow\n\nbody\n\n## Sub\n\nmore"


def test_headings_without_any_text_make_no_chunks() -> None:
    assert chunk_text("# A\n\n## B\n\n### C\n\n") == []


def test_heading_only_sections_inside_a_group_keep_their_heading_line() -> None:
    [chunk] = chunk_text("# A\n\n## B\n\n### C\n\ntext")
    assert chunk.heading_path == "A"
    assert chunk.content == "## B\n\n### C\n\ntext"


def test_merged_chunks_get_continuous_ordinals() -> None:
    text = "# A\n\n## a1\n\nx\n\n# B\n\n## b1\n\ny\n\n# C\n\nz"
    assert [c.ordinal for c in chunk_text(text)] == [0, 1, 2]


def test_small_paragraphs_are_packed_up_to_target() -> None:
    text = "# A\n\n" + "\n\n".join(f"kappale {i} " + "x" * 200 for i in range(20))
    chunks = chunk_text(text, target_tokens=100, max_tokens=160)
    assert len(chunks) > 1
    assert all(c.heading_path == "A" for c in chunks)
    assert all(len(c.content) <= 160 * 4 for c in chunks)


def test_overlap_between_consecutive_chunks() -> None:
    words = " ".join(f"sana{i}" for i in range(600))
    paras = "\n\n".join(words[i : i + 400] for i in range(0, len(words), 400))
    chunks = chunk_text(paras, target_tokens=100, max_tokens=200)
    assert len(chunks) > 2
    for prev, nxt in zip(chunks, chunks[1:], strict=False):
        tail_word = prev.content.split()[-1]
        assert tail_word in nxt.content  # overlap carries the end of the previous chunk


def test_oversized_paragraph_is_split_within_max() -> None:
    para = ". ".join(f"Lause numero {i} on tässä" for i in range(400)) + "."
    chunks = chunk_text(para, target_tokens=100, max_tokens=150)
    assert len(chunks) > 3
    assert all(len(c.content) <= 150 * 4 for c in chunks)


def test_unbroken_text_is_hard_cut() -> None:
    chunks = chunk_text("x" * 10_000, target_tokens=100, max_tokens=150)
    assert len(chunks) >= 2
    assert all(len(c.content) <= 150 * 4 for c in chunks)


def test_token_estimate_and_embedding_text() -> None:
    assert estimate_tokens("abcd" * 10) == 10
    assert estimate_tokens("") == 0
    assert embedding_text("Sivu", "A > B", "sisältö") == "Sivu — A > B\n\nsisältö"
    assert embedding_text(None, None, "sisältö") == "sisältö"
    assert embedding_text("Sivu", None, "x") == "Sivu\n\nx"
