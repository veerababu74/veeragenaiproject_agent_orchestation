from pinecone import Pinecone, ServerlessSpec

from config import settings
from rag_embeddings import EMBEDDING_DIMENSION


class VectorStoreError(Exception):
    pass


def _index():
    if not settings.pinecone_api_key:
        raise VectorStoreError("Pinecone is not configured")
    try:
        client = Pinecone(api_key=settings.pinecone_api_key)
        if not client.has_index(settings.pinecone_index):
            client.create_index(
                name=settings.pinecone_index,
                dimension=EMBEDDING_DIMENSION,
                metric="cosine",
                spec=ServerlessSpec(cloud="aws", region="us-east-1"),
            )
        return client.Index(settings.pinecone_index)
    except Exception as error:
        raise VectorStoreError("Could not connect to Pinecone") from error


def upsert(user_id: str, document_id: str, filename: str, chunks: list[str], embeddings: list[list[float]]) -> list[str]:
    vector_ids = [f"{document_id}:{index}" for index in range(len(chunks))]
    vectors = [
        {"id": vector_ids[index], "values": embeddings[index], "metadata": {
            "document_id": document_id, "filename": filename, "position": index, "text": chunk,
        }}
        for index, chunk in enumerate(chunks)
    ]
    try:
        index = _index()
        for start in range(0, len(vectors), 100):
            index.upsert(vectors=vectors[start:start + 100], namespace=user_id)
    except VectorStoreError:
        raise
    except Exception as error:
        raise VectorStoreError("Could not index document chunks in Pinecone") from error
    return vector_ids


def query(user_id: str, vector: list[float], top_k: int = 5) -> list[dict]:
    try:
        result = _index().query(namespace=user_id, vector=vector, top_k=top_k, include_values=False, include_metadata=True)
        return [{"score": match.score, **match.metadata} for match in result.matches]
    except VectorStoreError:
        raise
    except Exception as error:
        raise VectorStoreError("Could not search document chunks in Pinecone") from error


def delete_document(user_id: str, document_id: str, chunk_count: int) -> None:
    if chunk_count <= 0:
        return
    try:
        _index().delete(ids=[f"{document_id}:{index}" for index in range(chunk_count)], namespace=user_id)
    except VectorStoreError:
        raise
    except Exception as error:
        raise VectorStoreError("Could not delete document vectors from Pinecone") from error
