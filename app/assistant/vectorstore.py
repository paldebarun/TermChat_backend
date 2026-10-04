from __future__ import annotations

import threading

import chromadb

from app.config import get_settings


def search_filter(conversation_id: str, file_ids: list[str] | None) -> dict:
    """Chroma `where` for a search: the conversation, optionally narrowed to
    the files the caller may see (used for groups, where later joiners must
    not reach documents sent before they joined)."""
    if file_ids is None:
        return {"conversation_id": conversation_id}
    return {"$and": [{"conversation_id": conversation_id}, {"file_id": {"$in": file_ids}}]}


class ConversationVectorStore:
    def __init__(self) -> None:
        self._client = None
        self._collection = None
        self._lock = threading.Lock()

    def _collection_for_use(self):
        if self._collection is not None:
            return self._collection
        with self._lock:
            if self._collection is None:
                settings = get_settings()
                self._client = chromadb.HttpClient(
                    host=settings.chroma_host,
                    port=settings.chroma_port,
                )
                self._collection = self._client.get_or_create_collection(
                    name=settings.chroma_collection_name,
                    metadata={"hnsw:space": "cosine"},
                )
        return self._collection

    def upsert_document(
        self,
        *,
        conversation_id: str,
        file_id: str,
        filename: str,
        chunks: list[tuple[int, str]],
    ) -> None:
        if not chunks:
            return
        collection = self._collection_for_use()
        # conversation_id in the id: the same file sent to two different peers
        # must not overwrite the first conversation's chunks/metadata.
        ids = [f"{conversation_id}:{file_id}:{index}" for index, _ in chunks]
        documents = [content for _, content in chunks]
        metadatas = [
            {
                "conversation_id": conversation_id,
                "file_id": file_id,
                "filename": filename,
                "chunk_index": index,
            }
            for index, _ in chunks
        ]
        collection.upsert(ids=ids, documents=documents, metadatas=metadatas)

    def delete_file(self, file_id: str) -> None:
        self._collection_for_use().delete(where={"file_id": file_id})

    def search(
        self, *, conversation_id: str, query: str, limit: int, file_ids: list[str] | None = None
    ) -> list[dict]:
        if file_ids is not None and not file_ids:
            return []
        result = self._collection_for_use().query(
            query_texts=[query],
            n_results=limit,
            where=search_filter(conversation_id, file_ids),
            include=["documents", "metadatas", "distances"],
        )
        docs = result.get("documents", [[]])[0]
        metas = result.get("metadatas", [[]])[0]
        distances = result.get("distances", [[]])[0]
        return [
            {"content": doc, "metadata": meta, "distance": distance}
            for doc, meta, distance in zip(docs, metas, distances)
        ]


vector_store = ConversationVectorStore()
