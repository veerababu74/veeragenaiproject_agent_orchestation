from langchain_openai import ChatOpenAI
from langchain_anthropic import ChatAnthropic
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_groq import ChatGroq
from langchain_core.language_models import BaseChatModel

PROVIDER_MODELS = {
    'openai': ['gpt-4o', 'gpt-4o-mini', 'gpt-4-turbo', 'o1', 'o3-mini'],
    'groq': ['llama-3.3-70b-versatile', 'llama-3.1-8b-instant', 'mixtral-8x7b-32768'],
    'anthropic': ['claude-sonnet-4-20250514', 'claude-haiku-4-20250414'],
    'google_genai': ['gemini-2.0-flash', 'gemini-1.5-pro', 'gemini-1.5-flash'],
    'openrouter': ['openai/gpt-4o', 'anthropic/claude-sonnet-4', 'meta-llama/llama-3.3-70b-instruct'],
    'mistral': ['mistral-large-latest', 'mistral-small-latest', 'codestral-latest'],
}

def get_llm(provider, model, api_key, temperature=0.7, max_tokens=4096, base_url=''):
    kw = {'temperature': temperature, 'max_tokens': max_tokens}
    if provider == 'openai':
        return ChatOpenAI(model=model, api_key=api_key, **kw)
    elif provider == 'groq':
        return ChatGroq(model=model, api_key=api_key, **kw)
    elif provider == 'anthropic':
        return ChatAnthropic(model=model, api_key=api_key, **kw)
    elif provider == 'google_genai':
        return ChatGoogleGenerativeAI(model=model, google_api_key=api_key, **kw)
    elif provider == 'openrouter':
        return ChatOpenAI(model=model, api_key=api_key, base_url='https://openrouter.ai/api/v1',
                         default_headers={'HTTP-Referer': 'https://agent-orchestrator.dev'}, **kw)
    elif provider == 'mistral':
        return ChatOpenAI(model=model, api_key=api_key, base_url='https://api.mistral.ai/v1', **kw)
    else:
        if base_url:
            return ChatOpenAI(model=model, api_key=api_key, base_url=base_url, **kw)
        return ChatOpenAI(model=model, api_key=api_key, **kw)

def get_user_llm(user_id, provider, model, temperature=0.7, max_tokens=4096):
    from database import get_db
    conn = get_db()
    row = conn.execute('SELECT api_key, base_url FROM llm_configs WHERE user_id=? AND provider=? AND is_active=1 LIMIT 1', (user_id, provider)).fetchone()
    conn.close()
    if not row: return None
    return get_llm(provider, model, row['api_key'], temperature, max_tokens, row['base_url'] or '')

def get_user_api_key(user_id, provider):
    from database import get_db
    conn = get_db()
    row = conn.execute('SELECT api_key FROM llm_configs WHERE user_id=? AND provider=? AND is_active=1 LIMIT 1', (user_id, provider)).fetchone()
    conn.close()
    return row['api_key'] if row else None

def save_user_api_key(user_id, provider, api_key, base_url=''):
    """Upsert one provider key for a user. Shared by Settings and the agent
    form so a key entered while creating an agent is the same record Settings
    lists, and is swept by the same 48-hour retention rule."""
    from database import get_db
    import json, uuid
    from datetime import datetime
    api_key = (api_key or '').strip()
    if not api_key:
        return None
    now = datetime.utcnow().isoformat()
    conn = get_db()
    existing = conn.execute('SELECT id FROM llm_configs WHERE user_id=? AND provider=?', (user_id, provider)).fetchone()
    if existing:
        cid = existing['id']
        conn.execute('UPDATE llm_configs SET api_key=?,base_url=?,is_active=1,updated_at=? WHERE id=?',
                     (api_key, base_url, now, cid))
    else:
        cid = str(uuid.uuid4())
        conn.execute('INSERT INTO llm_configs (id,user_id,provider,api_key,base_url,models,is_active,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)',
                     (cid, user_id, provider, api_key, base_url, json.dumps([]), 1, now, now))
    conn.commit(); conn.close()
    return cid
