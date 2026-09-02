from urllib.parse import quote

import requests

EMBEDDING_DIMENSION = 768
EMBEDDING_MODELS = ("gemini-embedding-001", "text-embedding-004")
DEFAULT_EMBEDDING_MODEL = "gemini-embedding-001"


class EmbeddingError(Exception):
    pass


def embed_texts(api_key: str, model: str, texts: list[str], task_type: str) -> list[list[float]]:
    resource = f"models/{model.removeprefix('models/')}"
    all_embeddings = []
    for start in range(0, len(texts), 100):
        batch = texts[start:start + 100]
        payload = {
            "requests": [
                {
                    "model": resource,
                    "content": {"parts": [{"text": text}]},
                    "taskType": task_type,
                    "outputDimensionality": EMBEDDING_DIMENSION,
                }
                for text in batch
            ]
        }
        try:
            response = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/{quote(resource, safe='/')}:batchEmbedContents",
                headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
                json=payload,
                timeout=(10, 120),
            )
        except requests.RequestException as error:
            raise EmbeddingError("Could not reach Google Gemini embeddings") from error
        if not response.ok:
            if response.status_code in (401, 403):
                raise EmbeddingError("Google Gemini rejected the embedding API key")
            if response.status_code == 429:
                raise EmbeddingError("Google Gemini embedding quota was reached")
            raise EmbeddingError(f"Google Gemini embedding request failed with status {response.status_code}")
        try:
            batch_embeddings = [item["values"] for item in response.json()["embeddings"]]
        except (KeyError, TypeError, ValueError) as error:
            raise EmbeddingError("Google Gemini returned invalid embeddings") from error
        if len(batch_embeddings) != len(batch) or any(len(values) != EMBEDDING_DIMENSION for values in batch_embeddings):
            raise EmbeddingError(f"Google Gemini did not return {EMBEDDING_DIMENSION}-dimension embeddings")
        all_embeddings.extend(batch_embeddings)
    return all_embeddings
