from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from database import get_db
from auth import current_user_id
from rag_embeddings import embed_texts, EmbeddingError, EMBEDDING_MODELS, DEFAULT_EMBEDDING_MODEL
import rag_storage
import rag_vector_store
import uuid
from datetime import datetime, timedelta

router = APIRouter(prefix='/rag', tags=['rag'])
MAX_FILE_SIZE = 4 * 1024 * 1024
ALLOWED = {'pdf', 'doc', 'docx', 'txt'}


@router.get('/documents')
async def list_docs(user_id: str = Depends(current_user_id)):
    conn = get_db()
    docs = [dict(r) for r in conn.execute('SELECT * FROM rag_documents WHERE user_id=? ORDER BY created_at DESC', (user_id,)).fetchall()]
    conn.close()
    return docs


@router.get('/embedding-models')
async def embedding_models():
    return {'models': list(EMBEDDING_MODELS), 'default': DEFAULT_EMBEDDING_MODEL}


@router.post('/upload')
async def upload(
    file: UploadFile = File(...),
    embedding_model: str = Form(DEFAULT_EMBEDDING_MODEL),
    embedding_api_key: str = Form(...),
    user_id: str = Depends(current_user_id),
):
    ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else ''
    if ext not in ALLOWED:
        raise HTTPException(400, f'Unsupported file type: .{ext}')
    content = await file.read()
    if len(content) > MAX_FILE_SIZE:
        raise HTTPException(400, f'File too large ({len(content) / 1024 / 1024:.1f}MB). Max 4MB.')
    embedding_model = embedding_model.strip() or DEFAULT_EMBEDDING_MODEL
    if embedding_model not in EMBEDDING_MODELS:
        raise HTTPException(400, f"Unsupported embedding model. Choose one of: {', '.join(EMBEDDING_MODELS)}")
    if not embedding_api_key.strip():
        raise HTTPException(400, 'A Google Gemini API key is required to generate embeddings.')

    did = str(uuid.uuid4())
    now = datetime.utcnow()
    exp = now + timedelta(hours=48)
    ft = ext if ext != 'docx' else 'doc'
    remote_path = f'agent-orchestrator/users/{user_id}/documents/{did}.{ext}'

    conn = get_db()
    conn.execute(
        'INSERT INTO rag_documents (id,user_id,file_name,file_type,file_size,remote_path,embedding_model,status,expires_at,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)',
        (did, user_id, file.filename, ft, len(content), remote_path, embedding_model, 'processing', exp.isoformat(), now.isoformat()),
    )
    conn.commit()
    conn.close()

    try:
        text = _extract(content, ft)
        from langchain_text_splitters import RecursiveCharacterTextSplitter
        chunks = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=200).split_text(text)
        if not chunks:
            raise ValueError('No readable text was found in the document')
        embeddings = embed_texts(embedding_api_key.strip(), embedding_model, chunks, 'RETRIEVAL_DOCUMENT')
        rag_storage.upload(content, remote_path)
        try:
            rag_vector_store.upsert(user_id, did, file.filename, chunks, embeddings)
        except rag_vector_store.VectorStoreError:
            rag_storage.delete(remote_path)
            raise
        conn = get_db()
        conn.execute('UPDATE rag_documents SET status=?,chunk_count=? WHERE id=?', ('ready', len(chunks), did))
        conn.commit()
        conn.close()
    except (EmbeddingError, ValueError, rag_storage.BucketError, rag_vector_store.VectorStoreError) as e:
        conn = get_db()
        conn.execute('UPDATE rag_documents SET status=?,error_message=? WHERE id=?', ('error', str(e), did))
        conn.commit()
        conn.close()

    conn = get_db()
    r = dict(conn.execute('SELECT * FROM rag_documents WHERE id=?', (did,)).fetchone())
    conn.close()
    return r


@router.delete('/documents/{did}')
async def delete_doc(did: str, user_id: str = Depends(current_user_id)):
    conn = get_db()
    row = conn.execute('SELECT * FROM rag_documents WHERE id=? AND user_id=?', (did, user_id)).fetchone()
    if not row:
        conn.close()
        raise HTTPException(404, 'Not found')
    document = dict(row)
    conn.close()
    if document.get('status') == 'ready':
        try:
            rag_vector_store.delete_document(user_id, did, document.get('chunk_count') or 0)
        except rag_vector_store.VectorStoreError:
            pass
        if document.get('remote_path'):
            try:
                rag_storage.delete(document['remote_path'])
            except rag_storage.BucketError:
                pass
    conn = get_db()
    conn.execute('DELETE FROM rag_documents WHERE id=?', (did,))
    conn.commit()
    conn.close()
    return {'deleted': True}


def _extract(content, ft):
    if ft == 'txt':
        return content.decode('utf-8', errors='replace')
    elif ft == 'pdf':
        import io
        from pypdf import PdfReader
        return '\n'.join(p.extract_text() or '' for p in PdfReader(io.BytesIO(content)).pages)
    elif ft in ('doc', 'docx'):
        import io
        from docx import Document
        return '\n'.join(p.text for p in Document(io.BytesIO(content)).paragraphs)
    return ''
