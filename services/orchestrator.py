from langchain_core.messages import SystemMessage, HumanMessage, AIMessage
from langgraph.graph import StateGraph, END, START
from langgraph.graph.message import MessagesState
from langgraph.prebuilt import ToolNode, tools_condition
import json, time, uuid
from database import get_db
from services.llm_provider import get_user_llm
from services.tool_builder import get_agent_tools

def build_agent_graph(user_id, agent_id):
    conn = get_db()
    agent = conn.execute('SELECT * FROM agents WHERE id=? AND user_id=?', (agent_id, user_id)).fetchone()
    conn.close()
    if not agent: raise ValueError(f'Agent {agent_id} not found')
    llm = get_user_llm(user_id, agent['llm_provider'], agent['llm_model'], agent['temperature'], agent['max_tokens'])
    if not llm: raise ValueError(f"No API key for {agent['llm_provider']}. Add it in Settings.")
    tools = get_agent_tools(user_id, agent_id)
    if tools: llm_with_tools = llm.bind_tools(tools)
    else: llm_with_tools = llm

    def agent_node(state):
        sys_msg = SystemMessage(content=agent['system_prompt'] or 'You are a helpful AI assistant.')
        resp = llm_with_tools.invoke([sys_msg] + state['messages'])
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
    try:
        chain = build_agent_graph(user_id, agent_id)
        result = await chain.ainvoke({'messages': [HumanMessage(content=message)]})
        output = '\n'.join(m.content for m in result.get('messages', []) if hasattr(m, 'content') and isinstance(m.content, str)).strip()
        dur = int((time.time() - t0) * 1000)
        conn = get_db()
        conn.execute("UPDATE agent_executions SET output_text=?,status='completed',duration_ms=? WHERE id=?", (output, dur, eid))
        conn.commit(); conn.close()
        return {'execution_id': eid, 'status': 'completed', 'output': output, 'duration_ms': dur}
    except Exception as e:
        dur = int((time.time() - t0) * 1000)
        conn = get_db()
        conn.execute("UPDATE agent_executions SET status='error',error_message=?,duration_ms=? WHERE id=?", (str(e), dur, eid))
        conn.commit(); conn.close()
        return {'execution_id': eid, 'status': 'error', 'error': str(e), 'duration_ms': dur}
