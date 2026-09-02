from fastapi import APIRouter, Depends
from models import LlmConfigCreate
from database import get_db
from services.llm_provider import PROVIDER_MODELS
from auth import current_user_id
import json, uuid
from datetime import datetime

router = APIRouter(prefix='/settings', tags=['settings'])

@router.get('/providers')
async def providers():
    return [{'id':p,'name':p.replace('_',' ').title(),'models':m} for p,m in PROVIDER_MODELS.items()]

@router.get('/llm-configs')
async def list_configs(user_id: str = Depends(current_user_id)):
    conn = get_db()
    rows = []
    for r in conn.execute('SELECT * FROM llm_configs WHERE user_id=? ORDER BY created_at DESC', (user_id,)).fetchall():
        c = dict(r); k = c.pop('api_key')
        c['api_key_masked'] = k[:4]+'****'+k[-4:] if k and len(k)>8 else '****'
        c['models'] = json.loads(c['models']) if isinstance(c['models'],str) else c['models']
        rows.append(c)
    conn.close()
    return rows

@router.post('/llm-configs')
async def create_config(cfg: LlmConfigCreate, user_id: str = Depends(current_user_id)):
    now = datetime.utcnow().isoformat(); mj = json.dumps(cfg.models)
    conn = get_db()
    ex = conn.execute('SELECT id FROM llm_configs WHERE user_id=? AND provider=?', (user_id, cfg.provider)).fetchone()
    if ex:
        conn.execute('UPDATE llm_configs SET api_key=?,base_url=?,models=?,is_active=1,updated_at=? WHERE id=?',
                      (cfg.api_key, cfg.base_url, mj, now, ex['id'])); cid = ex['id']
    else:
        cid = str(uuid.uuid4())
        conn.execute('INSERT INTO llm_configs (id,user_id,provider,api_key,base_url,models,is_active,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)',
                      (cid, user_id, cfg.provider, cfg.api_key, cfg.base_url, mj, 1, now, now))
    conn.commit()
    r = dict(conn.execute('SELECT * FROM llm_configs WHERE id=?', (cid,)).fetchone())
    k = r.pop('api_key'); r['api_key_masked'] = k[:4]+'****'+k[-4:] if k and len(k)>8 else '****'
    r['models'] = json.loads(r['models']) if isinstance(r['models'],str) else r['models']
    conn.close()
    return r

@router.delete('/llm-configs/{cid}')
async def delete_config(cid: str, user_id: str = Depends(current_user_id)):
    conn = get_db()
    conn.execute('DELETE FROM llm_configs WHERE id=? AND user_id=?', (cid, user_id))
    conn.commit(); conn.close()
    return {'deleted': True}

@router.get('/stats')
async def stats(user_id: str = Depends(current_user_id)):
    conn = get_db()
    def cnt(t): return conn.execute(f'SELECT COUNT(*) as c FROM {t} WHERE user_id=?', (user_id,)).fetchone()['c']
    r = {'agents': cnt('agents'), 'tools': cnt('tools'), 'connections': cnt('agent_connections'),
         'rag_documents': cnt('rag_documents'), 'executions': conn.execute('SELECT COUNT(*) as c FROM agent_executions WHERE user_id=? AND status="completed"', (user_id,)).fetchone()['c']}
    conn.close()
    return r
