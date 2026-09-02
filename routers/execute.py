from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from models import ExecuteRequest
from services.orchestrator import execute_agent, stream_agent
from auth import current_user_id
import json

router = APIRouter(prefix='/execute', tags=['execute'])


def _assert_owned(user_id, agent_id):
    from database import get_db
    conn = get_db()
    found = conn.execute('SELECT id FROM agents WHERE id=? AND user_id=?', (agent_id, user_id)).fetchone()
    conn.close()
    if not found:
        raise HTTPException(404, 'Agent not found')


@router.post('')
async def run_agent(req: ExecuteRequest, user_id: str = Depends(current_user_id)):
    _assert_owned(user_id, req.agent_id)
    return await execute_agent(user_id, req.agent_id, req.message, req.conversation_id)


@router.post('/stream')
async def run_agent_stream(req: ExecuteRequest, user_id: str = Depends(current_user_id)):
    """Server-sent events: one `token` per chunk of the reply, a `delegation`
    for each question sent to a connected agent, then a final `done`."""
    _assert_owned(user_id, req.agent_id)

    async def events():
        try:
            async for event in stream_agent(user_id, req.agent_id, req.message, req.conversation_id):
                yield f'data: {json.dumps(event)}\n\n'
        except Exception as error:
            yield f'data: {json.dumps({"type": "error", "error": str(error)})}\n\n'

    return StreamingResponse(events(), media_type='text/event-stream', headers={
        'Cache-Control': 'no-cache',
        'Connection': 'keep-alive',
        'X-Accel-Buffering': 'no',  # keeps proxies from buffering the stream
    })


@router.get('/history')
async def history(user_id: str = Depends(current_user_id), agent_id: str = None, limit: int = 20):
    from database import get_db
    conn = get_db()
    if agent_id:
        rows = conn.execute('''SELECT e.*, a.name AS agent_name FROM agent_executions e
                               LEFT JOIN agents a ON a.id = e.agent_id
                               WHERE e.user_id=? AND e.agent_id=? ORDER BY e.created_at DESC LIMIT ?''',
                            (user_id, agent_id, limit)).fetchall()
    else:
        rows = conn.execute('''SELECT e.*, a.name AS agent_name FROM agent_executions e
                               LEFT JOIN agents a ON a.id = e.agent_id
                               WHERE e.user_id=? ORDER BY e.created_at DESC LIMIT ?''',
                            (user_id, limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@router.get('/conversations/{conversation_id}')
async def get_conversation(conversation_id: str, user_id: str = Depends(current_user_id)):
    from database import get_db
    conn = get_db()
    rows = conn.execute(
        'SELECT role, content, created_at FROM conversation_messages WHERE user_id=? AND conversation_id=? ORDER BY created_at, rowid',
        (user_id, conversation_id),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@router.delete('/conversations/{conversation_id}')
async def clear_conversation(conversation_id: str, user_id: str = Depends(current_user_id)):
    from database import get_db
    conn = get_db()
    conn.execute('DELETE FROM conversation_messages WHERE user_id=? AND conversation_id=?', (user_id, conversation_id))
    conn.commit(); conn.close()
    return {'cleared': True}
