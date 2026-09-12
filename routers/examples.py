"""Ready-made graphs, and loading one in a single call.

Loading an example is deliberately the same operation as importing an exported
workspace — it reuses `import_graph` rather than duplicating the insert logic,
so an example can never drift out of step with what import accepts. The only
extra work here is saving the user's model key and stamping their chosen model
onto every agent in the graph, because a graph whose agents all point at a
provider the user has no key for is a graph that fails on its first run.
"""

import json
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from auth import current_user_id
from database import get_db
from routers.agents import import_graph
from services.builtin_tools import BUILTIN_TOOLS
from services.examples import EXAMPLES, example_by_id, summary
from services.llm_provider import PROVIDER_MODELS

router = APIRouter(prefix='/examples', tags=['examples'])


def _wipe(conn, user_id):
    """Empty a user's workspace. Children first, because connections and tool
    assignments reference the agent rows they would otherwise orphan."""
    conn.execute('DELETE FROM agent_connections WHERE user_id=?', (user_id,))
    conn.execute(
        """DELETE FROM tool_assignments WHERE agent_id IN
           (SELECT id FROM agents WHERE user_id=?)""", (user_id,))
    conn.execute('DELETE FROM agents WHERE user_id=?', (user_id,))
    conn.execute('DELETE FROM tools WHERE user_id=?', (user_id,))


class LoadExampleRequest(BaseModel):
    provider: str = Field(default='openai', max_length=40)
    model: str = Field(default='', max_length=120)
    api_key: str = Field(default='', max_length=400)
    # Loading beside existing work is the safe default; a user who wants a clean
    # canvas has to ask for it, because the alternative silently deletes agents
    # they may have spent an hour on.
    replace: bool = False


@router.get('')
async def list_examples():
    """Every example, without its graph payload.

    The tool entries carry `needs_key` so the UI can say up front which parts of
    a graph will work immediately and which need a credential — discovering that
    when a tool silently fails mid-run is a bad way to learn it.
    """
    return {
        'examples': [summary(example) for example in EXAMPLES],
        'optional_tool_info': {
            tool_type: {
                'name': entry['name'],
                'description': entry['description'],
                'needs_key': bool(entry['requires']),
            }
            for tool_type, entry in BUILTIN_TOOLS.items()
        },
        'providers': [{'id': provider, 'models': models}
                      for provider, models in PROVIDER_MODELS.items()],
    }


@router.get('/{example_id}')
async def get_example(example_id: str):
    example = example_by_id(example_id)
    if not example:
        raise HTTPException(404, 'Unknown example')
    return {**summary(example), 'graph': example['graph']}


@router.post('/clear')
async def clear_workspace(user_id: str = Depends(current_user_id)):
    """Remove every agent, connection and tool this user has.

    Exposed separately from the replace flag because wanting an empty canvas is
    not the same as wanting a different example, and making someone load one in
    order to clear the last is a silly way to spend a click.
    """
    conn = get_db()
    try:
        removed = conn.execute(
            'SELECT COUNT(*) AS n FROM agents WHERE user_id=?', (user_id,)).fetchone()['n']
        _wipe(conn, user_id)
        conn.commit()
    finally:
        conn.close()
    return {'cleared': True, 'agents_removed': removed}


@router.post('/{example_id}/load')
async def load_example(example_id: str, request: LoadExampleRequest,
                       user_id: str = Depends(current_user_id)):
    """Create the example's agents, connections and tools for this user."""
    example = example_by_id(example_id)
    if not example:
        raise HTTPException(404, 'Unknown example')

    provider = request.provider if request.provider in PROVIDER_MODELS else 'openai'
    model = request.model.strip() or PROVIDER_MODELS[provider][0]
    now = datetime.utcnow().isoformat()
    conn = get_db()

    try:
        if request.api_key.strip():
            existing = conn.execute(
                'SELECT id FROM llm_configs WHERE user_id=? AND provider=?',
                (user_id, provider)).fetchone()
            models = json.dumps(PROVIDER_MODELS[provider])
            if existing:
                conn.execute(
                    'UPDATE llm_configs SET api_key=?,models=?,is_active=1,updated_at=? WHERE id=?',
                    (request.api_key.strip(), models, now, existing['id']))
            else:
                conn.execute(
                    '''INSERT INTO llm_configs (id,user_id,provider,api_key,base_url,models,
                       is_active,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?)''',
                    (str(uuid.uuid4()), user_id, provider, request.api_key.strip(), '',
                     models, 1, now, now))
            conn.commit()

        has_key = conn.execute(
            'SELECT 1 FROM llm_configs WHERE user_id=? AND provider=? AND api_key != ""',
            (user_id, provider)).fetchone() is not None

        if request.replace:
            _wipe(conn, user_id)
            conn.commit()
    finally:
        conn.close()

    # Point every agent at the provider and model the user actually has a key
    # for, rather than the example's own default.
    graph = example['graph']
    payload = {
        **graph,
        'agents': [{**agent, 'llm_provider': provider, 'llm_model': model}
                   for agent in graph['agents']],
    }
    result = await import_graph(payload, user_id)

    return {
        **result,
        'example_id': example_id,
        'title': example['title'],
        'mode': example['mode'],
        'provider': provider,
        'model': model,
        'has_key': has_key,
        'sample_tasks': example['sample_tasks'],
        'look_for': example['look_for'],
        'note': ('Loaded. Run one of the sample tasks against the lead agent to see the '
                 f"{example['mode']} mode working."
                 if has_key else
                 f'Loaded, but there is no {provider} API key saved yet — add one in Settings '
                 'before running it.'),
    }
