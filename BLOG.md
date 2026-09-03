# Building an Agent Orchestrator: What Actually Breaks When Agents Talk to Each Other

*A multi-agent platform where you draw a graph of AI agents, give them tools, and
chat with any of them. This is the story of building it — the design decisions,
and more usefully, the bugs that only appear once agents start calling each other.*

---

## The premise

Most "multi-agent" demos are a single agent with a fancy prompt. I wanted the real
thing: a canvas where you create agents, connect them, give each one tools, and
then talk to one of them — and it consults the others to answer you.

Three requirements shaped everything:

1. **It has to be visual.** Drag out an agent, drag a line to another, done.
2. **It has to be explainable.** When an agent gives you an answer, you should be
   able to see exactly which agents it consulted, which tools it ran, and *why* it
   chose them.
3. **It has to forget.** Everything a user creates — agents, documents, and their
   API keys — is deleted after 48 hours.

That third one turned out to shape the architecture more than anything else.

---

## Part 1: The pattern problem

The first real design question: when agent A is connected to agent B, what does
that *mean* at run time?

There are several established answers:

| Pattern | How it works |
|---|---|
| **Sequential pipeline** | A runs, feeds B, feeds C. Deterministic. |
| **Supervisor / router** | One agent decides which specialist to consult. |
| **Conditional** | Branch on some property of the input. |
| **Parallel fan-out** | Ask everyone at once, merge the answers. |

I started with **supervisor**, implemented as *agent-as-tool*. Each connected
agent becomes a tool on the agent you're chatting with:

```python
StructuredTool.from_function(
    coroutine=_run,
    name=f"ask_{slug}",                       # ask_weather_expert
    description=f"Ask the specialist agent '{name}' a question "
                f"and get its answer back. Use it for: {purpose}.",
    args_schema=DelegateInput,                # {question: str}
)
```

This is elegant. The model already knows how to choose tools, so it already knows
how to choose *agents*. It can consult one, several, or none. It can combine their
answers. You get routing for free from a capability the model already has.

It worked on the first try. Then a user tried it and reported that it didn't work
at all.

---

## Part 2: The bug that was really a design flaw

> *"I connect two or three agents, I ask the 3rd agent one question, it's not
> communicating to the other agents for the answers."*

My first instinct was that the delegation tools weren't being bound. They were. So
I wrote the smallest test that would reproduce the user's setup:

```python
a1, a2, a3 = agent('Researcher'), agent('Analyst'), agent('Final Answerer')
connect(a1, a3)   # work flows *toward* the final answerer
connect(a2, a3)

for name, agent_id in (('Researcher', a1), ('Analyst', a2), ('Final', a3)):
    print(name, '->', [p['name'] for p in _connected_agents(U, agent_id)])
```

```
Researcher       can consult -> ['Final Answerer']
Analyst          can consult -> ['Final Answerer']
Final Answerer   can consult -> NOTHING
```

There it is. My query followed **outgoing** edges:

```sql
WHERE c.source_agent_id = ?
```

The user had drawn arrows pointing *into* their final agent — because that's the
natural way to draw "these two feed the answerer." Their final agent had nothing to
consult, so it answered alone, silently.

I could have documented "draw your arrows this way." That would have been wrong.
When users consistently draw something one way, the software is holding the wrong
model. So a connection stopped being directional:

```sql
JOIN agents a ON a.id = CASE WHEN c.source_agent_id = :aid
                             THEN c.target_agent_id ELSE c.source_agent_id END
WHERE (c.source_agent_id = :aid OR c.target_agent_id = :aid)
```

**A connection now means "these two can talk."** Draw it either way; same result.

### But then: what about sequential?

Making edges undirected fixed the confusion and immediately created a new one. The
user came back with a sharper question: *is this actually doing sequential,
conditional, and the other orchestration patterns?*

No. It was doing exactly one pattern — supervisor — and calling it orchestration.
Worse, a `condition_expr` column had existed on every connection since the first
commit. It was stored, returned by the API, editable in the UI, and **never
evaluated by anything**. A field that looks like a feature and does nothing is
worse than a missing feature.

The fix was a per-agent `orchestration_mode`, with one rule that keeps it coherent:

> **In every mode except supervisor, the agent you chat with writes the final
> answer.** The mode only decides how its connected agents contribute first.

That single rule is what lets direction stay irrelevant. The graph describes
*capability*; the mode describes *procedure*. They're independent.

```
supervisor   the model is handed ask_B, ask_C as tools and picks
sequential   B runs, then C runs seeing what B said, then A composes
parallel     B and C run at once, then A merges
conditional  only agents whose condition matches run, then A composes
```

Conditional routing is one classification call covering *every* edge, not one per
edge:

```
Decide which of these agents are relevant to the question.
Reply with ONLY a JSON array of the matching numbers, e.g. [0,2].

Question: will it rain tomorrow?

Agents:
0. Weather: the question is about weather
1. Finance: the question is about markets
```

And it **fails open**. If the model returns prose instead of JSON, every
conditional agent contributes. Slower and more expensive — but a routing failure
should never leave the user with no answer at all. That single decision is the
difference between a flaky feature and a trustworthy one.

---

## Part 3: Recursion is not hypothetical

The moment agents can call agents, A → B → A is one careless click away. Two
independent guards, because one is never enough:

```python
# Guard 1 — depth. Past this, no delegate tools are offered at all.
MAX_DELEGATION_DEPTH = 3

# Guard 2 — cycles. `chain` accumulates everyone already in this path.
delegates = [t for t in _connected_agents(user_id, agent_id) if t['id'] not in chain]
```

Verified against a triangle where every agent connects to every other:

```
Alpha (depth 0)              -> ['ask_beta', 'ask_gamma']
Beta reached from Alpha      -> ['ask_gamma']
Gamma reached via Alpha,Beta -> <no tools bound>
```

Each hop offers strictly fewer options. The path cannot close.

---

## Part 4: The bug that had been there the whole time

This is the one worth reading.

I was writing a regression test that built an agent with a custom HTTP tool
attached — something no test had done before. It blew up:

```
pydantic.errors.PydanticUserError: A non-annotated attribute was detected:
`id = (<class 'str'>, FieldInfo(...))`. All model fields require a type annotation
```

The code that built a tool's argument schema from stored JSON:

```python
fields[key] = (field_type, Field(description=desc))
return type(schema_name, (BaseModel,), fields)      # ← wrong
```

`name: (type, FieldInfo)` is **`create_model`'s** signature. Passing it to `type()`
creates plain class attributes with no annotations, which Pydantic v2 rejects
outright.

The consequence was much worse than "custom tools don't work." `get_agent_tools`
had no error handling, so the exception propagated up through graph construction
and **killed the entire agent**. Attach a custom tool to an agent, and that agent
stops answering — with an error that points at Pydantic internals, nowhere near
the actual cause.

The fix is one line:

```python
return create_model(safe_name, **fields)
```

But the *lesson* is the second fix:

```python
try:
    tool = create_custom_tool(...) if custom else create_builtin_tool(...)
except Exception:
    logger.exception('Skipping tool %s (%s) that failed to build', row['name'], tt)
    continue
```

**One broken tool must not take the agent down with it.** The agent now runs with
the tools that do build. Degrading is almost always better than failing, and this
bug is the argument for it: a trivial schema mistake became total agent failure
purely because nothing was isolated.

Pulling that thread found four more problems in the same file:

- **GET dropped every argument.** `session.get(url, **kw)` never sent the model's
  arguments, so a `get_weather(city)` tool always called the bare URL and returned
  something unrelated. It looked like the model was being stupid. It wasn't.
- **PUT, PATCH and DELETE were rejected** — while the UI offered them in a dropdown.
- **A non-JSON response was reported as an error**, so any API returning plain text
  looked broken.
- **An HTTP 404 was returned as if it had succeeded**, because the body parsed as
  JSON. The model had no way to know the call failed.

None of these throw. They all silently produce a wrong answer, which is the worst
failure mode a tool can have.

---

## Part 5: Making it explainable

By this point the system could produce an answer by consulting three agents and
running five tools. When that answer is wrong, "the AI got it wrong" is not a
debuggable statement.

The naive approach is to instrument the orchestrator: log before the model call,
after the tool call, and so on. That falls apart immediately, because a delegated
agent runs *its own graph* — you'd have to thread trace state through every call.

The better answer was already in LangChain: **callbacks propagate down
automatically.**

```python
class RunTracer(AsyncCallbackHandler):
    async def on_llm_end(self, response, *, run_id=None, **kwargs):
        ...
        if text.strip() and tool_calls:
            self.add(REASONING, content=text,
                     data={'chose': [c.get('name') for c in tool_calls]})
```

One handler attached at the top captures everything beneath it, including inside
sub-agents, in the correct order. To know *which* agent produced an event, every
compiled graph is stamped with its own identity:

```python
return graph.compile().with_config({'metadata': {
    'agent_name': agent['name'], 'agent_id': agent_id, 'agent_depth': depth,
}})
```

A delegated agent compiles its own graph and overrides the stamp for its subtree.
Attribution falls out for free.

Here's a real recorded run:

```
 1 question                              What is today and how many days are left?
 2 reasoning    [Supervisor]             "The user asked about today, so I should
                                          ask the time specialist."
                                          → chose ask_time_specialist
 3 tool_call    [Supervisor]  ask_time_specialist  {"question": "What is today?"}
 4    reasoning [Time Specialist]        "I need the current date."
                                          → chose current_datetime
 5    tool_call [Time Specialist]  current_datetime  {}
 6    tool_result [Time Specialist]      {"date": "2026-09-02", "day": "Wednesday"}
 7 tool_result  [Supervisor]  ask_time_specialist  Today is Wednesday.
 8 reasoning    [Supervisor]             → chose calculator
 9 tool_call    [Supervisor]  calculator {"expression": "30-2"}
10 tool_result  [Supervisor]  calculator 28
11 answer                                Today is Wednesday, and that leaves 28 days.
```

The `reasoning` rows are the point. They pair the model's own words *at the moment
of deciding* with the exact tools that decision produced. That's the difference
between a log and an explanation.

---

## Part 6: Streaming, and one async trap

Two sources have to merge into one ordered stream: LangGraph's event iterator, and
a queue that delegate tools push into as they run.

My first version leaked events:

```python
# Wrong — the shielded get() survives the timeout and later
# consumes an event that nobody is listening for.
yield await asyncio.wait_for(asyncio.shield(queue.get()), timeout=0.25)
```

`shield` protects the inner task from cancellation, so on timeout the `queue.get()`
stays pending, eventually takes an item, and drops it on the floor. The fix is to
own exactly one getter and only cancel it when it has *not* completed:

```python
getter = asyncio.create_task(queue.get())
while True:
    done, _ = await asyncio.wait({getter, task}, return_when=asyncio.FIRST_COMPLETED)
    if getter in done:
        yield getter.result()
        getter = asyncio.create_task(queue.get())
        continue
    getter.cancel()          # nothing was taken, so nothing is lost
    while not queue.empty():
        yield queue.get_nowait()
    break
```

There's a second subtlety on the model side. Delegated agents run their own models,
whose token events propagate into the same stream — and would interleave into your
reply as gibberish. So the top-level model is tagged, and only its tokens are
streamed:

```python
if depth == 0:
    llm_with_tools = llm_with_tools.with_config({'tags': [PRIMARY_TAG]})
```

That filter looks decorative. It is load-bearing.

---

## Part 7: Designing around forgetting

The 48-hour retention rule started as a constraint and became a design driver.

The obvious part is the sweep. The non-obvious part is **ordering**:

```python
# External state first — the row is what tells us where it lives.
for doc in expiring_docs:
    rag_vector_store.delete_document(doc['user_id'], doc['id'], doc['chunk_count'])
    rag_storage.delete(doc['remote_path'])

# Only then the rows.
conn.execute("DELETE FROM rag_documents WHERE created_at < datetime('now','-48 hours')")
```

Delete the SQLite row first and you lose the `remote_path` and `chunk_count` you
need to clean up Pinecone and Hugging Face — orphaning data in two external systems
forever, invisibly.

The more interesting consequence: **if everything is deleted, users lose their work
every two days.** The answer wasn't to weaken the policy, it was export/import —
the whole workspace as JSON, agents and tools referenced by index so an import can
recreate them with fresh ids.

With one non-negotiable rule:

```python
SECRET_KEYS = {'api_key', 'token', 'bot_token', 'webhook_url', 'password', 'secret'}
```

**Credentials are stripped on export.** The file gets downloaded, emailed, committed
to a repo. Everything in it is re-enterable, so the safe default is the only
defensible one — and the UI says so after every import.

---

## What I'd tell someone building this

**Watch how people draw the diagram.** The direction bug wasn't a coding error; it
was my mental model losing to the user's. When users consistently do something
"wrong," the software is wrong.

**A field that exists but does nothing is a lie.** `condition_expr` sat in the
schema and the UI for weeks doing nothing. Either wire it up or delete it.

**Isolate every extension point.** One malformed custom tool killed an entire
agent because nothing was wrapped. Anything user-defined — a tool, a schema, a
condition — should be able to fail without taking the system with it.

**Silent wrongness is the enemy.** The GET-drops-arguments bug threw no error,
logged nothing, and produced a plausible-looking wrong answer. Those cost far more
than crashes. Surface HTTP status. Return text when it isn't JSON. Make failure
*visible*.

**Build the trace before you need it.** The moment more than one model is involved,
"why did it answer that?" stops being answerable by reading code. The callback
handler took an afternoon and has explained every confusing run since.

**Decide how each failure degrades.** Conditional routing fails open. A broken tool
is skipped. A failed run isn't written to memory. None of these are accidents, and
each one is the difference between a demo and something you'd let someone use.

---

*Built with FastAPI, LangGraph, LangChain, React and SQLite. Full technical
documentation is in [ARCHITECTURE.md](ARCHITECTURE.md).*
