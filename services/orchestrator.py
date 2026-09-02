from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.tools import StructuredTool
from langgraph.graph import StateGraph, END, START
from langgraph.graph.message import MessagesState
from langgraph.prebuilt import ToolNode, tools_condition
from pydantic import BaseModel, Field
import re, time, uuid
from database import get_db
from services.llm_provider import get_user_llm
from services.tool_builder import get_agent_tools

# How many agent hops a single question may take. The agent you chat with is
# depth 0, so this allows supervisor -> specialist -> specialist before the
# chain is cut off. Cycles are blocked separately by `chain`.
MAX_DELEGATION_DEPTH = 3


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
    """Agents this one may delegate to: the targets of its outgoing edges."""
    conn = get_db()
    rows = conn.execute(
        '''SELECT a.id, a.name, a.description, c.label
           FROM agent_connections c JOIN agents a ON a.id = c.target_agent_id
           WHERE c.source_agent_id = ? AND c.user_id = ? AND a.user_id = ?''',
        (agent_id, user_id, user_id),
    ).fetchall()
    conn.close()
    return [dict(row) for row in rows]


def _delegate_tool(user_id, target, depth, chain, trace):
    name = _tool_name(target['name'], target['id'])
    purpose = target.get('label') or target.get('description') or f"the {target['name']} agent"

    class DelegateInput(BaseModel):
        question: str = Field(description=f"The question to ask {target['name']}. Include all context it needs - it cannot see this conversation.")

    async def _run(question: str):
        trace.append({'agent': target['name'], 'role': 'request', 'text': question})
        try:
            graph = build_agent_graph(user_id, target['id'], depth + 1, chain + (target['id'],), trace)
            result = await graph.ainvoke({'messages': [HumanMessage(content=question)]})
            answer = _final_text(result) or 'The agent returned no answer.'
        except Exception as error:
            answer = f'{target["name"]} could not answer: {error}'
        trace.append({'agent': target['name'], 'role': 'response', 'text': answer})
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
        trace = trace if trace is not None else []
        delegates = [target for target in _connected_agents(user_id, agent_id) if target['id'] not in chain]
        tools = tools + [_delegate_tool(user_id, target, depth, chain, trace) for target in delegates]

    llm_with_tools = llm.bind_tools(tools) if tools else llm

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


async def execute_agent(user_id, agent_id, message):
    eid = str(uuid.uuid4())
    t0 = time.time()
    conn = get_db()
    conn.execute("INSERT INTO agent_executions (id,user_id,agent_id,input_text,status) VALUES (?,?,?,?,?)",
                 (eid, user_id, agent_id, message, 'running'))
    conn.commit(); conn.close()
    trace = []
    try:
        chain = build_agent_graph(user_id, agent_id, trace=trace)
        result = await chain.ainvoke({'messages': [HumanMessage(content=message)]})
        output = _final_text(result)
        dur = int((time.time() - t0) * 1000)
        conn = get_db()
        conn.execute("UPDATE agent_executions SET output_text=?,status='completed',duration_ms=? WHERE id=?", (output, dur, eid))
        conn.commit(); conn.close()
        return {'execution_id': eid, 'status': 'completed', 'output': output, 'duration_ms': dur, 'delegations': trace}
    except Exception as e:
        dur = int((time.time() - t0) * 1000)
        conn = get_db()
        conn.execute("UPDATE agent_executions SET status='error',error_message=?,duration_ms=? WHERE id=?", (str(e), dur, eid))
        conn.commit(); conn.close()
        return {'execution_id': eid, 'status': 'error', 'error': str(e), 'duration_ms': dur, 'delegations': trace}
