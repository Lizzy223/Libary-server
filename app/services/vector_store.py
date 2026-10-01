"""Pinecone vector store for semantic search, recommendations, and RAG context retrieval.

Uses Pinecone's own Inference API (llama-text-embed-v2) so no separate embedding
service or API key is needed beyond the Pinecone key itself.

All public functions degrade silently when Pinecone is not configured — the rest of
the system keeps working with RapidFuzz search and SQL-based recommendations.
"""
import logging
from functools import lru_cache

from ..config import get_config
from ..models import Title

log = logging.getLogger("library")

_EMBED_MODEL = "llama-text-embed-v2"
_DIMS = 1024
_METRIC = "cosine"
_BATCH = 96  # Pinecone recommends <= 100 vectors per upsert call


def is_configured() -> bool:
    return bool(get_config().pinecone_api_key)


@lru_cache(maxsize=1)
def _pc():
    from pinecone import Pinecone  # noqa: PLC0415
    return Pinecone(api_key=get_config().pinecone_api_key)


@lru_cache(maxsize=1)
def get_index():
    pc = _pc()
    name = get_config().pinecone_index
    existing = {idx.name for idx in pc.list_indexes()}
    if name not in existing:
        from pinecone import ServerlessSpec  # noqa: PLC0415
        pc.create_index(
            name=name,
            dimension=_DIMS,
            metric=_METRIC,
            spec=ServerlessSpec(cloud="aws", region="us-east-1"),
        )
        log.info("Created Pinecone index '%s'", name)
    return pc.Index(name)


def _text(title: Title) -> str:
    parts = [title.title, title.authors, title.subjects,
             title.description, title.subject_area, title.department]
    return " ".join(p for p in parts if p)


def _meta(title: Title) -> dict:
    return {
        "subject_area": title.subject_area or "",
        "department": title.department or "",
        "format": title.format or "",
        "language": title.language or "",
        "year": title.year or 0,
        "retired": bool(title.retired),
    }


def _embed(texts: list[str], input_type: str) -> list[list[float]]:
    result = _pc().inference.embed(
        model=_EMBED_MODEL,
        inputs=texts,
        parameters={"input_type": input_type},
    )
    return [r.values for r in result]


def upsert_title(title: Title) -> None:
    """Index or re-index a single title. Safe to call after every create/update."""
    if not is_configured():
        return
    try:
        vector = _embed([_text(title)], "passage")[0]
        get_index().upsert(vectors=[{"id": str(title.id), "values": vector, "metadata": _meta(title)}])
    except Exception as exc:
        log.warning("Pinecone upsert failed for title %s: %s", title.id, exc)


def upsert_batch(titles: list[Title]) -> None:
    """Bulk-index a list of titles. Used for startup sync and bulk imports."""
    if not is_configured() or not titles:
        return
    try:
        vectors = _embed([_text(t) for t in titles], "passage")
        records = [{"id": str(t.id), "values": v, "metadata": _meta(t)} for t, v in zip(titles, vectors)]
        for i in range(0, len(records), _BATCH):
            get_index().upsert(vectors=records[i:i + _BATCH])
        log.info("Upserted %d titles to Pinecone", len(titles))
    except Exception as exc:
        log.warning("Pinecone batch upsert failed: %s", exc)


def delete_title(title_id: int) -> None:
    """Remove a title from the index (called on retire)."""
    if not is_configured():
        return
    try:
        get_index().delete(ids=[str(title_id)])
    except Exception as exc:
        log.warning("Pinecone delete failed for title %s: %s", title_id, exc)


def semantic_search(query: str, filters: dict, top_k: int = 100) -> list[int]:
    """Return title IDs ranked by semantic relevance to query.

    Note: available_only cannot be checked here — caller must post-filter against the DB.
    """
    if not is_configured():
        return []
    try:
        vector = _embed([query], "query")[0]
        pf: dict = {"retired": {"$eq": False}}
        for key in ("subject_area", "department", "format", "language"):
            if filters.get(key):
                pf[key] = {"$eq": filters[key]}
        if filters.get("year"):
            pf["year"] = {"$eq": int(filters["year"])}
        result = get_index().query(vector=vector, top_k=top_k, filter=pf, include_metadata=False)
        return [int(m.id) for m in result.matches]
    except Exception as exc:
        log.warning("Pinecone semantic_search failed: %s", exc)
        return []


def similar_to(title_id: int, top_k: int = 20) -> list[int]:
    """Return IDs of titles semantically similar to the given one (excludes itself)."""
    if not is_configured():
        return []
    try:
        index = get_index()
        fetched = index.fetch(ids=[str(title_id)])
        vec_obj = fetched.vectors.get(str(title_id))
        if not vec_obj:
            return []
        result = index.query(
            vector=vec_obj.values,
            top_k=top_k + 1,
            filter={"retired": {"$eq": False}},
            include_metadata=False,
        )
        return [int(m.id) for m in result.matches if int(m.id) != title_id][:top_k]
    except Exception as exc:
        log.warning("Pinecone similar_to failed for title %s: %s", title_id, exc)
        return []
