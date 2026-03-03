"""
knowledge_loader.py — OceanTrust AI

Reads Markdown files from data/knowledge/, splits them into fixed-size chunks,
generates text embeddings via Google Generative AI (models/text-embedding-004),
and upserts the resulting vector points into the appropriate Qdrant collection.

Routing logic:
  - Filenames containing "law", "regulation", "quota", "legislation", "directive"
    → collection: fishing_regulations
  - Filenames containing "manual", "protocol", "disease", "biology", "health"
    → collection: biological_manuals
  - All others → collection: scientific_papers

Usage:
    python -m src.ingestion.knowledge_loader
    python -m src.ingestion.knowledge_loader --data-dir data/knowledge --dry-run

Environment variables (see .env.example):
    GOOGLE_API_KEY      Required — used to call Google Generative AI embeddings
    QDRANT_URL          Optional — defaults to http://localhost:6333
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import os
import time
import uuid
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Iterator

import structlog
from langchain_google_genai import GoogleGenerativeAIEmbeddings
from pydantic import BaseModel, Field
from qdrant_client import AsyncQdrantClient
from qdrant_client.models import PointStruct

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
structlog.configure(wrapper_class=structlog.make_filtering_bound_logger(logging.INFO))
log: structlog.BoundLogger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
QDRANT_URL: str    = os.getenv("QDRANT_URL", "http://localhost:6333")
EMBEDDING_MODEL: str = "models/gemini-embedding-001"  # Google Generative AI — 768-dim
EMBEDDING_DIM: int   = 768                             # gemini-embedding-001 default output dimension

CHUNK_SIZE_TOKENS: int   = 512   # Target tokens per chunk (approx. 4 chars/token)
CHUNK_OVERLAP_CHARS: int = 256   # ~64 tokens overlap
CHARS_PER_TOKEN: int     = 4     # Approximation used to convert token budget to chars

UPSERT_BATCH_SIZE: int = 50  # Points per Qdrant upsert request
EMBED_BATCH_SIZE: int  = 50  # Texts per Google Generative AI embeddings call

# ---------------------------------------------------------------------------
# Collection routing keywords
# Each tuple: (collection_name, list_of_filename_keywords)
# ---------------------------------------------------------------------------
COLLECTION_ROUTING: list[tuple[str, list[str]]] = [
    ("fishing_regulations", ["law", "regulation", "quota", "legislation", "directive"]),
    ("biological_manuals",  ["manual", "protocol", "disease", "biology", "health"]),
    ("scientific_papers",   []),  # Catch-all
]


# ---------------------------------------------------------------------------
# Domain models
# ---------------------------------------------------------------------------

class ChunkPayload(BaseModel):
    """Qdrant payload schema — aligns with docs/knowledge-ingestion.md §5."""

    doc_id: str
    source_hash: str
    chunk_index: int = Field(ge=0)
    chunk_total: int = Field(ge=1)
    chunk_text_preview: str = Field(max_length=200)
    collection: str
    document_type: str = "REGULATION"
    jurisdiction: list[str] = Field(default_factory=list)
    species: list[str] = Field(default_factory=list)
    language: str = "en"
    effective_date: str = str(date.today())  # ISO date string
    expiry_date: str | None = None
    superseded_by_doc_id: str | None = None
    is_active: bool = True
    title: str
    authority: str = "Unknown"
    article_reference: str | None = None
    page_number: int | None = None
    source_url: str
    ingested_at: str = ""      # Filled at runtime
    embedding_model: str = EMBEDDING_MODEL


@dataclass
class DocumentMetadata:
    """Metadata extracted from a Markdown front-matter table."""

    doc_id: str
    title: str
    jurisdiction: list[str]
    species: list[str]
    document_type: str
    authority: str
    effective_date: str
    language: str
    source_url: str


# ---------------------------------------------------------------------------
# Chunker
# ---------------------------------------------------------------------------

def chunk_markdown(text: str) -> Iterator[str]:
    """
    Split Markdown text into overlapping chunks using a paragraph-first strategy.

    Separator hierarchy:
        \\n\\n\\n  → major section break
        \\n\\n    → paragraph break
        \\n      → line break
        . (sentence) → last resort

    Target: ~CHUNK_SIZE_TOKENS tokens (~CHUNK_SIZE_TOKENS * CHARS_PER_TOKEN chars).
    Overlap: CHUNK_OVERLAP_CHARS characters from the end of the previous chunk.
    """
    target_chars = CHUNK_SIZE_TOKENS * CHARS_PER_TOKEN  # ~2048 chars
    separators   = ["\n\n\n", "\n\n", "\n", ". "]

    def _split(t: str, sep_idx: int) -> list[str]:
        if sep_idx >= len(separators):
            return [t]
        sep   = separators[sep_idx]
        parts = t.split(sep)
        return [p.strip() for p in parts if p.strip()]

    paragraphs = _split(text, 0)
    current: list[str] = []
    current_len: int   = 0
    prev_tail: str     = ""

    for para in paragraphs:
        if current_len + len(para) > target_chars and current:
            chunk_text = "\n\n".join(current)
            yield prev_tail + chunk_text
            prev_tail = (
                chunk_text[-CHUNK_OVERLAP_CHARS:] + "\n\n"
                if len(chunk_text) > CHUNK_OVERLAP_CHARS else ""
            )
            current     = []
            current_len = 0

        current.append(para)
        current_len += len(para)

    if current:
        yield prev_tail + "\n\n".join(current)


# ---------------------------------------------------------------------------
# Metadata parser
# ---------------------------------------------------------------------------

def extract_metadata(text: str, file_path: Path) -> DocumentMetadata:
    """
    Extract structured metadata from the Markdown front-matter table.

    Expected table format (first table in the document):
        | Field       | Value  |
        |-------------|--------|
        | **doc_id**  | ...    |
        ...

    Falls back to filename-derived defaults if parsing fails.
    """
    def _find_value(label: str, lines: list[str]) -> str:
        label_clean = label.lower().strip("* ")
        for line in lines:
            cells = [c.strip().strip("*") for c in line.split("|") if c.strip()]
            if len(cells) >= 2 and cells[0].lower() == label_clean:
                return cells[1].strip()
        return ""

    lines = text.splitlines()

    doc_id       = _find_value("doc_id",        lines) or f"DOC-{file_path.stem.upper()}"
    title        = _find_value("title",         lines) or file_path.stem.replace("_", " ").title()
    authority    = _find_value("authority",     lines) or "Unknown"
    jurisdiction = [j.strip() for j in _find_value("jurisdiction", lines).split(",") if j.strip()]
    species      = [s.strip() for s in _find_value("species",      lines).split(",") if s.strip()]
    doc_type     = _find_value("document_type", lines) or "REGULATION"
    effective    = _find_value("effective_date", lines) or str(date.today())
    language     = _find_value("language",      lines) or "en"
    source_url   = _find_value("source_url",    lines) or f"file://{file_path.resolve()}"

    return DocumentMetadata(
        doc_id=doc_id,
        title=title,
        jurisdiction=jurisdiction or ["EU_GENERAL"],
        species=species or ["ALL"],
        document_type=doc_type,
        authority=authority,
        effective_date=effective,
        language=language,
        source_url=source_url,
    )


# ---------------------------------------------------------------------------
# Collection router
# ---------------------------------------------------------------------------

def route_to_collection(file_path: Path) -> str:
    """
    Determine the target Qdrant collection from the filename.
    Ref: docs/knowledge-ingestion.md §2 — Collection routing.
    """
    name_lower = file_path.stem.lower()
    for collection, keywords in COLLECTION_ROUTING:
        if any(kw in name_lower for kw in keywords):
            return collection
    return "scientific_papers"


# ---------------------------------------------------------------------------
# Embedder — Google Generative AI (models/text-embedding-004, 768-dim)
# ---------------------------------------------------------------------------

def _build_embedder() -> GoogleGenerativeAIEmbeddings:
    """Instantiate GoogleGenerativeAIEmbeddings from the GOOGLE_API_KEY env var."""
    return GoogleGenerativeAIEmbeddings(
        model=EMBEDDING_MODEL,
        google_api_key=os.environ["GOOGLE_API_KEY"],
    )


async def embed_texts(embedder: GoogleGenerativeAIEmbeddings, texts: list[str]) -> list[list[float]]:
    """
    Asynchronously embed a batch of texts using Google Generative AI.

    GoogleGenerativeAIEmbeddings.aembed_documents() is the async-native method.
    Returns a list of 768-dimensional float vectors, one per input text, in the
    same order as the input list — compatible with the existing Qdrant PointStruct schema.
    """
    vectors: list[list[float]] = await embedder.aembed_documents(texts)
    log.info(
        "embeddings_generated",
        count=len(vectors),
        dim=EMBEDDING_DIM,
        model=EMBEDDING_MODEL,
    )
    return vectors


# ---------------------------------------------------------------------------
# SHA-256 hash (deduplication key)
# ---------------------------------------------------------------------------

def sha256_hex(content: str) -> str:
    """Return the SHA-256 hex digest of the UTF-8 encoded content string."""
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Core ingestion logic
# ---------------------------------------------------------------------------

async def ingest_file(
    file_path: Path,
    embedder: GoogleGenerativeAIEmbeddings,
    qdrant_client: AsyncQdrantClient,
    dry_run: bool = False,
) -> int:
    """
    Full ingestion pipeline for a single Markdown file.

    Steps:
      1. Read file content.
      2. Extract metadata from front-matter table.
      3. Chunk using the paragraph-first recursive splitter.
      4. Generate embeddings in batches via Google Generative AI.
      5. Build ChunkPayload and PointStruct for each chunk.
      6. Upsert to the routed Qdrant collection.

    Returns:
        Number of chunks successfully upserted.
    """
    log.info("processing_file", path=str(file_path))
    raw_text = file_path.read_text(encoding="utf-8")

    meta        = extract_metadata(raw_text, file_path)
    collection  = route_to_collection(file_path)
    source_hash = sha256_hex(raw_text)
    ingested_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    chunks: list[str] = list(chunk_markdown(raw_text))
    chunk_total = len(chunks)
    log.info("chunks_created", doc_id=meta.doc_id, count=chunk_total, collection=collection)

    if dry_run:
        log.info("dry_run_skip", doc_id=meta.doc_id, chunks=chunk_total)
        return chunk_total

    points: list[PointStruct] = []

    # Embed in batches of EMBED_BATCH_SIZE to respect API rate limits
    for batch_start in range(0, chunk_total, EMBED_BATCH_SIZE):
        batch_texts = chunks[batch_start : batch_start + EMBED_BATCH_SIZE]
        embeddings  = await embed_texts(embedder, batch_texts)

        for local_idx, (chunk_text, vector) in enumerate(zip(batch_texts, embeddings)):
            global_idx = batch_start + local_idx
            point_id   = str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{meta.doc_id}::chunk::{global_idx}"))

            payload = ChunkPayload(
                doc_id=meta.doc_id,
                source_hash=source_hash,
                chunk_index=global_idx,
                chunk_total=chunk_total,
                chunk_text_preview=chunk_text[:200],
                collection=collection,
                document_type=meta.document_type,
                jurisdiction=meta.jurisdiction,
                species=meta.species,
                language=meta.language,
                effective_date=meta.effective_date,
                is_active=True,
                title=meta.title,
                authority=meta.authority,
                source_url=meta.source_url,
                ingested_at=ingested_at,
                embedding_model=EMBEDDING_MODEL,
            )

            points.append(PointStruct(
                id=point_id,
                vector=vector,
                payload=payload.model_dump(),
            ))

    # Upsert in batches of UPSERT_BATCH_SIZE
    upserted = 0
    for batch_start in range(0, len(points), UPSERT_BATCH_SIZE):
        batch = points[batch_start : batch_start + UPSERT_BATCH_SIZE]
        await qdrant_client.upsert(collection_name=collection, points=batch)
        upserted += len(batch)
        log.info("batch_upserted", collection=collection, batch_size=len(batch), total_so_far=upserted)

    log.info("file_ingested", doc_id=meta.doc_id, collection=collection, total_chunks=upserted)
    return upserted


async def ingest_directory(
    data_dir: Path,
    embedder: GoogleGenerativeAIEmbeddings,
    qdrant_client: AsyncQdrantClient,
    dry_run: bool = False,
) -> None:
    """
    Discover all .md files under data_dir recursively and ingest each one.
    Processes files sequentially to respect Google Generative AI rate limits.
    """
    md_files = sorted(data_dir.rglob("*.md"))

    if not md_files:
        log.warning("no_markdown_files_found", data_dir=str(data_dir))
        return

    log.info("discovery_complete", file_count=len(md_files), data_dir=str(data_dir))

    total_chunks = 0
    for file_path in md_files:
        try:
            n = await ingest_file(file_path, embedder, qdrant_client, dry_run=dry_run)
            total_chunks += n
        except Exception:
            log.exception("file_ingestion_failed", path=str(file_path))

    log.info("ingestion_complete", total_files=len(md_files), total_chunks=total_chunks)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

async def main(data_dir: Path, dry_run: bool) -> None:
    """
    Validate environment, initialise clients, and run the ingestion pipeline.
    Raises EnvironmentError if GOOGLE_API_KEY is not set (unless --dry-run).
    """
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key and not dry_run:
        raise EnvironmentError(
            "GOOGLE_API_KEY environment variable is not set. "
            "Copy .env.example to .env and add your Google API key, "
            "or run with --dry-run to validate chunking without embedding."
        )

    embedder      = _build_embedder() if not dry_run else None   # type: ignore[assignment]
    qdrant_client = AsyncQdrantClient(url=QDRANT_URL)

    log.info(
        "knowledge_loader_starting",
        data_dir=str(data_dir),
        qdrant_url=QDRANT_URL,
        embedding_model=EMBEDDING_MODEL,
        embedding_dim=EMBEDDING_DIM,
        dry_run=dry_run,
    )

    await ingest_directory(data_dir, embedder, qdrant_client, dry_run=dry_run)
    await qdrant_client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="OceanTrust AI — Knowledge Loader: embed Markdown docs into Qdrant."
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=Path("data/knowledge"),
        help="Root directory containing Markdown knowledge base files (default: data/knowledge)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Parse and chunk files without calling Google AI or writing to Qdrant",
    )
    args = parser.parse_args()
    asyncio.run(main(data_dir=args.data_dir, dry_run=args.dry_run))
