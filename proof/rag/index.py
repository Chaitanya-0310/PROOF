"""
Parse the SOP corpus into section chunks and index them into Postgres.

Two decisions carry this whole phase:

**Chunks are SECTIONS, not fixed-size windows.** A 512-token sliding window
produces chunks that begin mid-sentence and belong to no particular part of
the document, so the strongest citation you can offer is a filename. SOPs
already carry numbered headings written by the people who will be asked to
verify the answer, so cutting on those headings means every chunk arrives
with an anchor a quality manager can look up in the binder. "SOP-RESTART-FLAT
section 4.2" is checkable; "SOP-RESTART-FLAT, somewhere" is not.

**Embeddings are computed locally on CPU.** all-MiniLM-L6-v2, 384 dims. No
document text leaves the machine to build this index. That is the same
argument Phase 4 makes for the hybrid LLM split, applied one layer down --
and a food manufacturer's procedures are exactly the kind of document that
never gets approved for a third-party embedding API.

Run:  make index
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from proof.db import connect  # noqa: E402

CORPUS = Path(__file__).resolve().parent.parent.parent / "corpus" / "sops"

EMBED_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
EMBED_DIM = 384

# A '## 1. Hazard' or '## 4.2 Proof window verification' heading.
#
# The trailing dot is optional because real procedure documents are not
# typographically consistent -- this corpus has both forms, written by hand,
# which is exactly what a plant's document set looks like. The number is the
# citation anchor, so a document with no numbered headings is REJECTED rather
# than indexed with a citation nobody can look up.
HEADING = re.compile(r"^##\s+(?P<no>\d+(?:\.\d+)*)\.?\s+(?P<title>.+?)\s*$", re.M)


@dataclass
class Chunk:
    doc_id: str
    section_no: str
    section_title: str
    ordinal: int
    content: str
    citation: str


def parse_front_matter(text: str) -> tuple[dict, str]:
    """Split YAML-ish front matter from the body.

    Hand-rolled rather than pulling in PyYAML: the front matter is five scalar
    fields and two string lists, and a dependency for that is not worth the
    install. If the corpus ever needs real YAML this is the place to swap it.
    """
    if not text.startswith("---"):
        raise ValueError("missing front matter")
    _, fm, body = text.split("---", 2)
    meta: dict = {}
    for line in fm.strip().splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            meta[key] = [v.strip() for v in inner.split(",") if v.strip()]
        else:
            meta[key] = value
    return meta, body.strip()


def chunk_document(doc_id: str, body: str) -> list[Chunk]:
    """Cut a document on its numbered '## N.N Title' headings."""
    matches = list(HEADING.finditer(body))
    if not matches:
        raise ValueError(f"{doc_id}: no numbered '## N Title' headings found; "
                         f"sections are the citation anchors and cannot be skipped")

    chunks: list[Chunk] = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        content = body[start:end].strip()
        if not content:
            continue
        no, title = m.group("no"), m.group("title")
        chunks.append(Chunk(
            doc_id=doc_id,
            section_no=no,
            section_title=title,
            ordinal=i,
            # Heading text is prepended to the indexed content. A section
            # titled "Wet allergen wash" whose body never repeats the phrase
            # is otherwise invisible to lexical search for "allergen wash".
            content=f"{no} {title}\n{content}",
            citation=f"{doc_id} §{no}",
        ))
    return chunks


def load_corpus() -> tuple[list[dict], list[Chunk]]:
    docs, chunks = [], []
    for path in sorted(CORPUS.glob("*.md")):
        meta, body = parse_front_matter(path.read_text(encoding="utf-8"))
        doc_id = meta["doc_id"]
        docs.append({
            "doc_id": doc_id,
            "title": meta["title"],
            "doc_type": meta["doc_type"],
            "version": meta["version"],
            "effective_date": meta["effective_date"],
            "owner": meta["owner"],
            "applies_to_categories": meta.get("applies_to_categories", []),
            "applies_to_lines": meta.get("applies_to_lines", []),
            "source_path": f"corpus/sops/{path.name}",
            "body": body,
        })
        chunks.extend(chunk_document(doc_id, body))
    return docs, chunks


def embed(texts: list[str]):
    """Embed on CPU. Imported lazily so the rest of the module stays cheap."""
    from sentence_transformers import SentenceTransformer

    model = SentenceTransformer(EMBED_MODEL, device="cpu")
    # Normalised vectors: with unit length, cosine distance and inner product
    # agree, and pgvector's <=> operator is then a straightforward 1 - cosine.
    return model.encode(texts, normalize_embeddings=True,
                        show_progress_bar=False, batch_size=32)


def main() -> None:
    print(f"Indexing SOP corpus from {CORPUS}")
    docs, chunks = load_corpus()
    print(f"  {len(docs)} documents -> {len(chunks)} section chunks")

    by_doc: dict[str, int] = {}
    for c in chunks:
        by_doc[c.doc_id] = by_doc.get(c.doc_id, 0) + 1
    widest = max(by_doc, key=lambda k: by_doc[k])
    print(f"  sections per document: min {min(by_doc.values())}, "
          f"max {by_doc[widest]} ({widest})")

    print(f"  embedding on CPU with {EMBED_MODEL} ...")
    vectors = embed([c.content for c in chunks])
    print(f"  {len(vectors)} vectors of dim {len(vectors[0])}")
    if len(vectors[0]) != EMBED_DIM:
        raise SystemExit(f"model returned dim {len(vectors[0])}, "
                         f"but docs.chunks declares vector({EMBED_DIM})")

    with connect("seeder") as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE docs.chunks, docs.documents CASCADE")
            cur.executemany("""
                INSERT INTO docs.documents (doc_id, title, doc_type, version,
                    effective_date, owner, applies_to_categories,
                    applies_to_lines, source_path, body)
                VALUES (%(doc_id)s, %(title)s, %(doc_type)s, %(version)s,
                        %(effective_date)s, %(owner)s, %(applies_to_categories)s,
                        %(applies_to_lines)s, %(source_path)s, %(body)s)
            """, docs)
            cur.executemany("""
                INSERT INTO docs.chunks (doc_id, section_no, section_title,
                    ordinal, content, citation, token_estimate, embedding)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """, [
                (c.doc_id, c.section_no, c.section_title, c.ordinal, c.content,
                 c.citation,
                 # ~4 chars per token is close enough to size a context
                 # budget; nothing depends on it being exact.
                 max(1, len(c.content) // 4),
                 "[" + ",".join(f"{v:.6f}" for v in vec) + "]")
                for c, vec in zip(chunks, vectors)
            ])
        conn.commit()
    print("  indexed.")


if __name__ == "__main__":
    main()
