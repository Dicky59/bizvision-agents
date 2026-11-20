from core.ingest.chunking import chunk_text, embedding_text, estimate_tokens


def test_empty_input() -> None:
    assert chunk_text("") == []
    assert chunk_text("  \n\n ") == []


def test_heading_paths_follow_hierarchy() -> None:
    text = "intro\n\n# Palvelut\n\nyleistä\n\n## AI-asiakaspalvelija\n\nbotti\n\n# Yhteys\n\nsoita"
    chunks = chunk_text(text)
    assert [(c.heading_path, c.content) for c in chunks] == [
        (None, "intro"),
        ("Palvelut", "yleistä"),
        ("Palvelut > AI-asiakaspalvelija", "botti"),
        ("Yhteys", "soita"),
    ]
    assert [c.ordinal for c in chunks] == [0, 1, 2, 3]


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
