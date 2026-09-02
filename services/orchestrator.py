from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import StructuredTool
from langgraph.graph import StateGraph, END, START
from langgraph.graph.message import MessagesState
from langgraph.prebuilt import ToolNode, tools_condition
from pydantic import BaseModel, Field
import asyncio, re, time, uuid
from datetime import datetime
from database import get_db
from services.llm_provider import get_user_llm
from services.tool_builder import get_agent_tools

# How many earlier turns of a conversation are replayed to the agent. Bounded so
# a long chat cannot grow the prompt without limit.
HISTORY_LIMIT = 20

# Tags the top-level agent's model so streaming can tell its tokens apart from
# those of agents it delegates to, which run their own graphs underneath.
PRIMARY_TAG = 'primary_agent'

# How many agent hops a single question may take. The agent you chat with is
# depth 0, so this allows supervisor -> specialist -> specialist before the
# chain is cut off. Cycles are blocked separately by `chain`.
MAX_DELEGATION_DEPTH = 3


class Trace(list):
    """Records delegation steps. When a queue is attached (the streaming path)
    each step is also pushed to it as it happens, so the client sees which agent
    is being consulted while the run is still going."""

    def __init__(self, queue=None):
        super().__init__()
        self.queue = queue

    def add(self, step):
        self.append(step)
        if self.queue is not None:
            self.queue.put_nowait({'type': 'delegation', **step})


def _tool_name(agent_name, agent_id):
    """LangChain tool names must be [a-zA-Z0-9_-]. Fall back to the agent id
    when a name has no usable characters (e.g. it is entirely emoji)."""
    slug = re.sub(r'[^a-zA-Z0-9_]+', '_', (agent_name or '').strip().lower()).strip('_')
    return f'ask_{slug}' if slug else f'ask_agent_{agent_id[:8]}'


def _final_text(result):
    """The answer is the last AI message with real text - not every message in
    the transcript, which would echo the question and raw tool output back."""
    for message in reversed(result.get('messages', [])):
        if isinstance(message, AIMessage) and isinstance(message.content, str) and message.content.strip():
            return message.content.strip()
    for message in reversed(result.get('messages', [])):
        if isinstance(message.content, str) and message.content.strip():
            return message.content.strip()
    return ''


def _connected_agents(user_id, agent_id):
    """Agents this one may consult: everything directly connected to it.

    Direction is deliberately ignored. People draw these graphs both ways -
    arrows flowing *into* the agent they intend to chat with is just as natural
    as out of it - and following only outgoing edges left the final agent in a
    chain with nobody to ask. A link means "these two can talk"; the cycle guard
    in build_agent_graph is what stops A -> B -> A.
    """
    conn = get_db()
    rows = conn.execute(
        '''SELECT a.id, a.name, a.description, c.label
           FROM agent_connections c
           JOIN agents a ON a.id = CASE WHEN c.source_agent_id = :aid
                                        THEN c.target_agent_id ELSE c.source_agent_id END
           WHERE (c.source_agent_id = :aid OR c.target_agent_id = :aid)
             AND c.user_id = :uid AND a.user_id = :uid AND a.id != :aid''',
        {'aid': agent_id, 'uid': user_id},
    ).fetchall()
    conn.close()
    # Two agents can be linked more than once; each should appear as one tool.
    unique = {}
    for row in rows:
        entry = dict(row)
        existing = unique.get(entry['id'])
        if existing is None or (not existing.get('label') and entry.get('label')):
            unique[entry['id']] = entry
    return list(unique.values())


def _delegate_tool(user_id, target, depth, chain, trace):
    name = _tool_name(target['name'], target['id'])
    purpose = target.get('label') or target.get('description') or f"the {target['name']} agent"

    class DelegateInput(BaseModel):
        question: str = Field(description=f"The question to ask {target['name']}. Include all context it needs - it cannot see this conversation.")

    async def _run(question: str):
        trace.add({'agent': target['name'], 'role': 'request', 'text': question})
        try:
            graph = build_agent_graph(user_id, target['id'], depth + 1, chain + (target['id'],), trace)
            result = await graph.ainvoke({'messages': [HumanMessage(content=question)]})
            answer = _final_text(result) or 'The agent returned no answer.'
        except Exception as error:
            answer = f'{target["name"]} could not answer: {error}'
        trace.add({'agent': target['name'], 'role': 'response', 'text': answer})
        return answer

    return StructuredTool.from_function(
        coroutine=_run, name=name, args_schema=DelegateInput,
        description=f"Ask the specialist agent '{target['name']}' a question and get its answer back. Use it for: {purpose}.",
    )


def build_agent_graph(user_id, agent_id, depth=0, chain=(), trace=None):
    conn = get_db()
    agent = conn.execute('SELECT * FROM agents WHERE id=? AND user_id=?', (agent_id, user_id)).fetchone()
    conn.close()
    if not agent: raise ValueError(f'Agent {agent_id} not found')
    llm = get_user_llm(user_id, agent['llm_provider'], agent['llm_model'], agent['temperature'], agent['max_tokens'])
    if not llm: raise ValueError(f"No API key for {agent['llm_provider']}. Add it on the agent or in Settings.")
    tools = get_agent_tools(user_id, agent_id)

    # Each connected agent becomes a tool, so the model itself decides which
    # one to consult and can combine several answers into its reply.
    delegates = []
    if depth < MAX_DELEGATION_DEPTH:
        chain = chain or (agent_id,)
        trace = trace if trace is not None else Trace()
        delegates = [target for target in _connected_agents(user_id, agent_id) if target['id'] not in chain]
        tools = tools + [_delegate_tool(user_id, target, depth, chain, trace) for target in delegates]

    llm_with_tools = llm.bind_tools(tools) if tools else llm
    if depth == 0:
        llm_with_tools = llm_with_tools.with_config({'tags': [PRIMARY_TAG]})

    base_prompt = agent['system_prompt'] or 'You are a helpful AI assistant.'
    if delegates:
        roster = '\n'.join(f"- {_tool_name(d['name'], d['id'])}: {d['name']}"
                           + (f" - {d['label'] or d['description']}" if (d['label'] or d['description']) else '')
                           for d in delegates)
        base_prompt += (
            '\n\nYou coordinate a team of specialist agents. You can ask any of them a question '
            'using its tool, and you may ask several before answering:\n' + roster +
            '\n\nDelegate whenever a question falls in a specialist\'s area, then combine what they '
            'return into one clear final answer for the user. Never mention the delegation itself.'
        )

    def agent_node(state):
        resp = llm_with_tools.invoke([SystemMessage(content=base_prompt)] + state['messages'])
        return {'messages': [resp]}

    graph = StateGraph(MessagesState)
    graph.add_node('agent', agent_node)
    if tools:
        graph.add_node('tools', ToolNode(tools))
        graph.add_conditional_edges('agent', tools_condition, {'tools': 'tools', END: END})
        graph.add_edge('tools', 'agent')
    else:
        graph.add_edge('agent', END)
    graph.add_edge(START, 'agent')
    return graph.compile()


def load_history(user_id, conversation_id):
    """Earlier turns of this conversation, oldest first, as LangChain messages."""
    if not conversation_id:
        return []
    conn = get_db()
    rows = conn.execute(
        '''SELECT role, content FROM conversation_messages
           WHERE user_id=? AND conversation_id=? ORDER BY created_at DESC, rowid DESC LIMIT ?''',
        (user_id, conversation_id, HISTORY_LIMIT),
    ).fetchall()
    conn.close()
    messages = []
    for row in reversed(rows):
        content = row['content'] or ''
        if content:
            messages.append(HumanMessage(content=content) if row['role'] == 'user' else AIMessage(content=content))
    return messages


def save_turn(user_id, conversation_id, agent_id, question, answer):
    if not conversation_id:
        return
    now = datetime.utcnow().isoformat()
    conn = get_db()
    conn.executemany(
        'INSERT INTO conversation_messages (id,user_id,conversation_id,agent_id,role,content,created_at) VALUES (?,?,?,?,?,?,?)',
        [(str(uuid.uuid4()), user_id, conversation_id, agent_id, 'user', question, now),
         (str(uuid.uuid4()), user_id, conversation_id, agent_id, 'assistant', answer, now)],
    )
    conn.commit(); conn.close()


def _start_execution(user_id, agent_id, message):
    eid = str(uuid.uuid4())
    conn = get_db()
    conn.execute("INSERT INTO agent_executions (id,user_id,agent_id,input_text,status) VALUES (?,?,?,?,?)",
                 (eid, user_id, agent_id, message, 'running'))
    conn.commit(); conn.close()
    return eid


def _finish_execution(eid, output, duration_ms, error=None):
    conn = get_db()
    if error is None:
        conn.execute("UPDATE agent_executions SET output_text=?,status='completed',duration_ms=? WHERE id=?", (output, duration_ms, eid))
    else:
        conn.execute("UPDATE agent_executions SET status='error',error_message=?,duration_ms=? WHERE id=?", (error, duration_ms, eid))
    conn.commit(); conn.close()


async def execute_agent(user_id, agent_id, message, conversation_id=None):
    eid = _start_execution(user_id, agent_id, message)
    t0 = time.time()
    trace = Trace()
    try:
        graph = build_agent_graph(user_id, agent_id, trace=trace)
        history = load_history(user_id, conversation_id)
        result = await graph.ainvoke({'messages': history + [HumanMessage(content=message)]})
        output = _final_text(result)
        dur = int((time.time() - t0) * 1000)
        _finish_execution(eid, output, dur)
        save_turn(user_id, conversation_id, agent_id, message, output)
        return {'execution_id': eid, 'status': 'completed', 'output': output, 'duration_ms': dur, 'delegations': list(trace)}
    except Exception as e:
        dur = int((time.time() - t0) * 1000)
        _finish_execution(eid, '', dur, error=str(e))
        return {'execution_id': eid, 'status': 'error', 'error': str(e), 'duration_ms': dur, 'delegations': list(trace)}


async def stream_agent(user_id, agent_id, message, conversation_id=None):
    """Yield run events as they happen: tokens from the agent being chatted
    with, and a step for every delegated question and answer."""
    eid = _start_execution(user_id, agent_id, message)
    t0 = time.time()
    queue = asyncio.Queue()
    trace = Trace(queue)
    yield {'type': 'start', 'execution_id': eid}

    try:
        graph = build_agent_graph(user_id, agent_id, trace=trace)
        history = load_history(user_id, conversation_id)
    except Exception as error:
        _finish_execution(eid, '', int((time.time() - t0) * 1000), error=str(error))
        yield {'type': 'error', 'error': str(error)}
        return

    chunks = []
    final_state = {}

    async def run():
        nonlocal final_state
        async for event in graph.astream_events(
            {'messages': history + [HumanMessage(content=message)]}, version='v2'
        ):
            kind = event.get('event')
            # Only the top-level agent's tokens are shown; delegated agents run
            # their own models and would interleave unreadably.
            if kind == 'on_chat_model_stream' and PRIMARY_TAG in (event.get('tags') or []):
                text = getattr(event['data'].get('chunk'), 'content', '')
                if isinstance(text, str) and text:
                    chunks.append(text)
                    queue.put_nowait({'type': 'token', 'text': text})
            elif kind == 'on_chain_end' and event.get('name') == 'LangGraph' and not event.get('parent_ids'):
                # Delegated agents run their own graph, which ends with the same
                # event name; only the root run's output is this reply.
                final_state = event['data'].get('output') or {}

    task = asyncio.create_task(run())
    try:
        while True:
            drain = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait({drain, task}, return_when=asyncio.FIRST_COMPLETED)
            if drain in done:
                yield drain.result()
                continue
            drain.cancel()
            while not queue.empty():
                yield queue.get_nowait()
            await task  # re-raises whatever the run failed with
            break
    except Exception as error:
        task.cancel()
        _finish_execution(eid, '', int((time.time() - t0) * 1000), error=str(error))
        yield {'type': 'error', 'error': str(error), 'delegations': list(trace)}
        return

    output = _final_text(final_state) or ''.join(chunks).strip()
    dur = int((time.time() - t0) * 1000)
    _finish_execution(eid, output, dur)
    save_turn(user_id, conversation_id, agent_id, message, output)
    yield {'type': 'done', 'execution_id': eid, 'output': output, 'duration_ms': dur, 'delegations': list(trace)}
