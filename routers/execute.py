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


@router.get('/runs/{execution_id}')
async def get_run(execution_id: str, user_id: str = Depends(current_user_id)):
    """One run with its full timeline: the question, what the agent reasoned,
    every tool it chose and what came back, and the final answer."""
    from database import get_db
    from services.tracing import load_trace
    conn = get_db()
    row = conn.execute('''SELECT e.*, a.name AS agent_name FROM agent_executions e
                          LEFT JOIN agents a ON a.id = e.agent_id
                          WHERE e.id=? AND e.user_id=?''', (execution_id, user_id)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, 'Run not found')
    trace = load_trace(user_id, execution_id)
    tool_events = [e for e in trace if e['event_type'] == 'tool_call']
    return {
        'run': dict(row),
        'trace': trace,
        'summary': {
            'steps': len(trace),
            'tool_calls': len(tool_events),
            'tools_used': sorted({e['name'] for e in tool_events if not e['name'].startswith('ask_')}),
            'agents_involved': sorted({e['agent'] for e in trace if e['agent']}),
            'delegations': len([e for e in tool_events if e['name'].startswith('ask_')]),
            'max_depth': max([e['depth'] for e in trace] or [0]),
            'errors': len([e for e in trace if e['event_type'] in ('tool_error', 'error')]),
        },
    }


@router.get('/metrics')
async def metrics(user_id: str = Depends(current_user_id), limit: int = 100):
    """Aggregates across recent runs, for the observability dashboard."""
    from database import get_db
    conn = get_db()
    runs = [dict(r) for r in conn.execute(
        'SELECT status, duration_ms, tokens_used, agent_id FROM agent_executions WHERE user_id=? ORDER BY created_at DESC LIMIT ?',
        (user_id, limit))]
    tool_rows = conn.execute(
        '''SELECT name, COUNT(*) AS calls, AVG(duration_ms) AS avg_ms,
                  SUM(CASE WHEN event_type='tool_error' THEN 1 ELSE 0 END) AS errors
           FROM run_events WHERE user_id=? AND event_type IN ('tool_call','tool_error')
           GROUP BY name ORDER BY calls DESC LIMIT 20''', (user_id,)).fetchall()
    agent_rows = conn.execute(
        '''SELECT a.name, COUNT(*) AS runs, AVG(e.duration_ms) AS avg_ms,
                  SUM(CASE WHEN e.status='error' THEN 1 ELSE 0 END) AS errors
           FROM agent_executions e JOIN agents a ON a.id = e.agent_id
           WHERE e.user_id=? GROUP BY a.name ORDER BY runs DESC LIMIT 20''', (user_id,)).fetchall()
    conn.close()
    completed = [r for r in runs if r['status'] == 'completed']
    durations = sorted(r['duration_ms'] or 0 for r in completed)
    return {
        'runs': len(runs),
        'completed': len(completed),
        'errors': len([r for r in runs if r['status'] == 'error']),
        'success_rate': round(100 * len(completed) / len(runs)) if runs else 0,
        'avg_duration_ms': round(sum(durations) / len(durations)) if durations else 0,
        'p95_duration_ms': durations[int(len(durations) * 0.95) - 1] if durations else 0,
        'total_tokens': sum(r['tokens_used'] or 0 for r in runs),
        'by_tool': [dict(r) for r in tool_rows],
        'by_agent': [dict(r) for r in agent_rows],
    }


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
