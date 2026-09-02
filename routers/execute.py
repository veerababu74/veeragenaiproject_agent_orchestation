from fastapi import APIRouter, Depends, HTTPException
from models import ExecuteRequest
from services.orchestrator import execute_agent
from auth import current_user_id

router = APIRouter(prefix='/execute', tags=['execute'])

@router.post('')
async def run_agent(req: ExecuteRequest, user_id: str = Depends(current_user_id)):
    from database import get_db
    conn = get_db()
    if not conn.execute('SELECT id FROM agents WHERE id=? AND user_id=?', (req.agent_id, user_id)).fetchone():
        conn.close(); raise HTTPException(404, 'Agent not found')
    conn.close()
    return await execute_agent(user_id, req.agent_id, req.message)

@router.get('/history')
async def history(user_id: str = Depends(current_user_id), agent_id: str = None, limit: int = 20):
    from database import get_db
    conn = get_db()
    if agent_id:
        rows = conn.execute('SELECT * FROM agent_executions WHERE user_id=? AND agent_id=? ORDER BY created_at DESC LIMIT ?', (user_id,agent_id,limit)).fetchall()
    else:
        rows = conn.execute('SELECT * FROM agent_executions WHERE user_id=? ORDER BY created_at DESC LIMIT ?', (user_id,limit)).fetchall()
    conn.close()
    return [dict(r) for r in rows]
