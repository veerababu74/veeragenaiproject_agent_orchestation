"""Run tracing: what the agent thought, chose, ran, and answered.

Implemented as a LangChain callback handler rather than by instrumenting the
orchestrator by hand, because callbacks propagate down into the graphs that
delegated agents run - so a sub-agent's own reasoning and tool calls land in
the same timeline, already in the right order, with no extra plumbing.

Events are buffered in memory during a run and written once at the end: a run
is short, and one transaction keeps the sequence contiguous. When a queue is
attached (the streaming path) each event is also pushed as it happens.
"""

import json
import time
import uuid
from datetime import datetime

from langchain_core.callbacks import AsyncCallbackHandler

from database import get_db

# What a single step in a run can be. The UI groups and colours by these.
QUESTION = 'question'
REASONING = 'reasoning'
TOOL_CALL = 'tool_call'
TOOL_RESULT = 'tool_result'
TOOL_ERROR = 'tool_error'
ANSWER = 'answer'
ERROR = 'error'

MAX_TEXT = 4000  # keep one oversized tool payload from bloating a trace


def _clip(value, limit=MAX_TEXT):
    if value is None:
        return ''
    text = value if isinstance(value, str) else json.dumps(value, default=str)
    return text if len(text) <= limit else text[:limit] + f'... [truncated, {len(text)} chars]'


class RunTracer(AsyncCallbackHandler):
    """Records an ordered timeline of one execution."""

    def __init__(self, execution_id, user_id, queue=None):
        self.execution_id = execution_id
        self.user_id = user_id
        self.queue = queue
        self.events = []
        self._seq = 0
        self._started = {}      # run_id -> (monotonic start, tool name, agent)
        self.tokens_in = 0
        self.tokens_out = 0

    # ── recording ───────────────────────────────────────────────────────────

    def add(self, event_type, *, name='', content='', data=None, duration_ms=0, agent=None, depth=0):
        self._seq += 1
        event = {
            'seq': self._seq,
            'event_type': event_type,
            'name': name or '',
            'agent': (agent or {}).get('name', '') if isinstance(agent, dict) else (agent or ''),
            'agent_id': (agent or {}).get('id', '') if isinstance(agent, dict) else '',
            'depth': depth,
            'content': _clip(content),
            'data': data or {},
            'duration_ms': duration_ms,
            'created_at': datetime.utcnow().isoformat(),
        }
        self.events.append(event)
        if self.queue is not None:
            self.queue.put_nowait({'type': 'trace', **event})
        return event

    @staticmethod
    def _agent_of(metadata):
        """Which agent this callback belongs to. build_agent_graph stamps the
        compiled graph with these, and nested graphs override them for their
        own subtree."""
        metadata = metadata or {}
        return {'name': metadata.get('agent_name', ''), 'id': metadata.get('agent_id', '')}, metadata.get('agent_depth', 0) or 0

    # ── model callbacks ─────────────────────────────────────────────────────

    async def on_chat_model_start(self, serialized, messages, *, run_id=None, metadata=None, **kwargs):
        self._started[run_id] = (time.monotonic(), '', metadata)

    async def on_llm_end(self, response, *, run_id=None, **kwargs):
        started, _, metadata = self._started.pop(run_id, (time.monotonic(), '', {}))
        agent, depth = self._agent_of(metadata)
        duration = int((time.monotonic() - started) * 1000)

        message = None
        for generations in getattr(response, 'generations', []) or []:
            for generation in generations:
                message = getattr(generation, 'message', None) or message

        usage = getattr(message, 'usage_metadata', None) or {}
        self.tokens_in += usage.get('input_tokens', 0) or 0
        self.tokens_out += usage.get('output_tokens', 0) or 0

        text = getattr(message, 'content', '')
        text = text if isinstance(text, str) else _clip(text)
        tool_calls = getattr(message, 'tool_calls', None) or []

        # The reasoning worth showing is what the model said *while* deciding to
        # use tools. Its final answer is recorded separately as ANSWER, so
        # skipping it here avoids showing the same text twice.
        if text.strip() and tool_calls:
            self.add(REASONING, content=text, agent=agent, depth=depth, duration_ms=duration,
                     data={'chose': [call.get('name') for call in tool_calls],
                           'tokens': {'in': usage.get('input_tokens', 0), 'out': usage.get('output_tokens', 0)}})
        elif tool_calls:
            self.add(REASONING, content='', agent=agent, depth=depth, duration_ms=duration,
                     data={'chose': [call.get('name') for call in tool_calls], 'no_text': True,
                           'tokens': {'in': usage.get('input_tokens', 0), 'out': usage.get('output_tokens', 0)}})

    # ── tool callbacks ──────────────────────────────────────────────────────

    async def on_tool_start(self, serialized, input_str, *, run_id=None, metadata=None, inputs=None, **kwargs):
        name = (serialized or {}).get('name') or 'tool'
        agent, depth = self._agent_of(metadata)
        self._started[run_id] = (time.monotonic(), name, metadata)
        self.add(TOOL_CALL, name=name, agent=agent, depth=depth,
                 content=_clip(inputs if inputs is not None else input_str, 1000),
                 data={'args': inputs if isinstance(inputs, dict) else {'input': _clip(input_str, 1000)},
                       'delegation': name.startswith('ask_')})

    async def on_tool_end(self, output, *, run_id=None, **kwargs):
        started, name, metadata = self._started.pop(run_id, (time.monotonic(), 'tool', {}))
        agent, depth = self._agent_of(metadata)
        content = getattr(output, 'content', output)
        self.add(TOOL_RESULT, name=name, agent=agent, depth=depth, content=_clip(content),
                 duration_ms=int((time.monotonic() - started) * 1000),
                 data={'delegation': name.startswith('ask_')})

    async def on_tool_error(self, error, *, run_id=None, **kwargs):
        started, name, metadata = self._started.pop(run_id, (time.monotonic(), 'tool', {}))
        agent, depth = self._agent_of(metadata)
        self.add(TOOL_ERROR, name=name, agent=agent, depth=depth, content=str(error),
                 duration_ms=int((time.monotonic() - started) * 1000))

    # ── persistence ─────────────────────────────────────────────────────────

    def flush(self):
        if not self.events:
            return
        rows = [(str(uuid.uuid4()), self.execution_id, self.user_id, e['seq'], e['event_type'], e['name'],
                 e['agent'], e['agent_id'], e['depth'], e['content'], json.dumps(e['data'], default=str),
                 e['duration_ms'], e['created_at'])
                for e in self.events]
        conn = get_db()
        conn.executemany(
            '''INSERT INTO run_events (id,execution_id,user_id,seq,event_type,name,agent,agent_id,depth,
               content,data,duration_ms,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)''', rows)
        conn.commit(); conn.close()
        self.events = []


def load_trace(user_id, execution_id):
    conn = get_db()
    rows = conn.execute(
        'SELECT * FROM run_events WHERE user_id=? AND execution_id=? ORDER BY seq',
        (user_id, execution_id)).fetchall()
    conn.close()
    trace = []
    for row in rows:
        event = dict(row)
        try:
            event['data'] = json.loads(event['data'] or '{}')
        except ValueError:
            event['data'] = {}
        trace.append(event)
    return trace
