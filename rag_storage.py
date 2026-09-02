import os
from pathlib import Path
from tempfile import gettempdir


def _configure_cache() -> None:
    if os.getenv("VERCEL"):
        cache = Path(gettempdir()) / "agent_orchestrator" / "huggingface"
        cache.mkdir(parents=True, exist_ok=True)
        os.environ["HF_HOME"] = str(cache)
        os.environ["HF_XET_CACHE"] = str(cache / "xet")


_configure_cache()

from huggingface_hub import HfApi

from config import settings


class BucketError(Exception):
    pass


def upload(content: bytes, remote_path: str) -> None:
    if not settings.huggingface_token:
        raise BucketError("Hugging Face storage is not configured")
    try:
        HfApi(token=settings.huggingface_token).batch_bucket_files(
            settings.huggingface_bucket, add=[(content, remote_path)]
        )
    except Exception as error:
        raise BucketError("Could not upload the original document to Hugging Face") from error


def delete(remote_path: str) -> None:
    if not settings.huggingface_token:
        raise BucketError("Hugging Face storage is not configured")
    try:
        HfApi(token=settings.huggingface_token).batch_bucket_files(
            settings.huggingface_bucket, delete=[remote_path]
        )
    except Exception as error:
        raise BucketError("Could not delete the original document from Hugging Face") from error
