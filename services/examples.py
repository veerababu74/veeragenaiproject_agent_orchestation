"""Ready-made multi-agent graphs.

Building a graph from scratch means creating four agents, writing four system
prompts, drawing the connections, picking an orchestration mode and attaching
tools — twenty-odd interactions before anything runs. That is a lot to ask of
someone who has not yet seen what the project does, and most people quite
reasonably give up before the first execution.

So each example here is a complete, working graph in exactly the shape
`POST /agents/import` already accepts: agents by index, connections referencing
those indices, tools, and assignments. Loading one is a single call, and the
only thing the user has to supply is their own model key.

**One example per orchestration mode, on purpose.** The four modes are the whole
idea of the project and the difference between them is invisible in a
description — supervisor, sequential, parallel and conditional produce genuinely
different traces for the same question. Each example is built so that its mode
is the obvious right choice for its task, and running two of them back to back
is the fastest way to understand why the setting exists.

Tools are keyless wherever possible (`web_fetch`, `calculator`, `datetime`,
`http_request`) so an example runs with nothing but a model key. Where a graph is
genuinely better with a credentialed tool, it is listed in `optional_tools` and
the graph works without it.
"""

# Laid out for the canvas: a lead agent on the left, its workers to the right,
# spaced so the imported graph is readable without anyone dragging nodes around.
_LEAD = (80, 240)
_COLUMN = 430
_ROW = 150


def _agent(name, description, prompt, x, y, mode="supervisor", model="gpt-4o-mini",
           temperature=0.3, sub=False):
    return {
        "name": name,
        "description": description,
        "system_prompt": prompt,
        "llm_provider": "openai",
        "llm_model": model,
        "temperature": temperature,
        "max_tokens": 2048,
        "is_sub_agent": sub,
        "position_x": x,
        "position_y": y,
        "orchestration_mode": mode,
    }


EXAMPLES = [
    # ── supervisor ──────────────────────────────────────────────────────────
    {
        "id": "research-desk",
        "title": "Research Desk",
        "tagline": "A lead that decides which specialist to ask, and when",
        "mode": "supervisor",
        "what_it_shows": (
            "Supervisor mode: the lead agent reads the question and chooses which of its "
            "connected agents to consult, in whatever order it decides. Nothing about the "
            "sequence is hard-coded — ask a question that only needs one specialist and it "
            "will only call one."
        ),
        "look_for": (
            "In the trace, the lead's delegation calls. On a broad question it usually asks the "
            "researcher first and the fact checker second; on a narrow one it often skips a step "
            "entirely. That choice is the model's, not the graph's."
        ),
        "needs_key": False,
        "optional_tools": ["tavily"],
        "sample_tasks": [
            "What is retrieval-augmented generation, and what is the main criticism of it?",
            "Summarise what a vector database does and when you would not need one.",
            "Explain the difference between fine-tuning and prompting, and check the claims.",
        ],
        "graph": {
            "agents": [
                _agent(
                    "Research Lead",
                    "Decides what needs investigating and who should do it",
                    "You lead a small research desk. You do not research anything yourself — you "
                    "delegate. Read the question, decide which of your connected specialists can "
                    "answer which part of it, and ask them. Ask the fact checker whenever an answer "
                    "contains a claim that could be wrong. When you have what you need, write the "
                    "final answer yourself in plain language, and say explicitly which parts came "
                    "from a specialist and which are your own synthesis.",
                    *_LEAD, mode="supervisor"),
                _agent(
                    "Web Researcher",
                    "Reads pages and reports what they actually say",
                    "You find and read sources. Open pages properly rather than guessing from a "
                    "title. Report what a source actually claims, quote the key sentence, and give "
                    "the URL. If you could not find something, say so — never fill the gap from "
                    "memory, because the whole point of asking you is that you looked.",
                    _COLUMN, 150, sub=True),
                _agent(
                    "Fact Checker",
                    "Challenges claims and separates fact from framing",
                    "You check claims. For each one you are given, say whether it is supported, "
                    "unsupported, or contested, and why. Be specific about what would change your "
                    "mind. Flag confident-sounding statements that rest on nothing. You are more "
                    "useful when you are sceptical than when you are agreeable.",
                    _COLUMN, 330, sub=True),
            ],
            "connections": [
                {"source": 0, "target": 1, "label": "find sources"},
                {"source": 0, "target": 2, "label": "check claims"},
            ],
            "tools": [
                {"name": "Web Page Reader", "description": "Fetch a URL and read its text",
                 "tool_type": "web_fetch", "is_builtin": True, "config": {}},
            ],
            "assignments": [{"agent": 1, "tool": 0}],
        },
    },

    # ── sequential ──────────────────────────────────────────────────────────
    {
        "id": "content-pipeline",
        "title": "Content Pipeline",
        "tagline": "Four agents in a fixed order, each improving the last one's work",
        "mode": "sequential",
        "what_it_shows": (
            "Sequential mode: every connected agent runs, in order, and each one receives what "
            "the previous produced. There is no decision to make — the order is the design. This "
            "is the right shape when each stage genuinely depends on the one before it."
        ),
        "look_for": (
            "That the draft visibly changes between stages. The strategist's angle survives into "
            "the writer's draft, the editor cuts rather than rewrites, and the critic argues with "
            "the result instead of praising it. If a stage adds nothing, it should not be there."
        ),
        "needs_key": False,
        "optional_tools": [],
        "sample_tasks": [
            "Write a short post explaining why chunk size matters in RAG systems.",
            "Draft a launch announcement for a feature that automates compliance reporting.",
            "Write an explainer on why agents need bounded loops, for a technical audience.",
        ],
        "graph": {
            "agents": [
                _agent(
                    "Strategist",
                    "Decides the angle before anyone writes",
                    "You decide what a piece is actually about before anyone writes it. Given a "
                    "topic, produce: the single claim the piece will make, who it is for, what "
                    "that reader already believes, and the one thing they should think differently "
                    "afterwards. Be decisive — pick an angle rather than listing options. Keep it "
                    "under 150 words. You are not writing the piece.",
                    *_LEAD, mode="sequential"),
                _agent(
                    "Writer",
                    "Turns the angle into a draft",
                    "You write the draft from the strategist's angle. Follow the angle you were "
                    "given rather than inventing your own. Open on the reader's problem, not on "
                    "your subject. Short sentences, concrete nouns, no hype words, no exclamation "
                    "marks. Every claim gets a reason or an example.",
                    _COLUMN, 120, temperature=0.6, sub=True),
                _agent(
                    "Editor",
                    "Cuts, and only cuts",
                    "You edit by removing. Cut every sentence that does not earn its place, every "
                    "adverb doing the work a verb should do, and every hedge. Do not add ideas and "
                    "do not rewrite the voice — if a sentence is fine, leave it exactly as it is. "
                    "Return the edited piece, then one line saying what you cut and why.",
                    _COLUMN, 270, sub=True),
                _agent(
                    "Critic",
                    "Argues with the finished piece",
                    "You are the reader who disagrees. Name the weakest claim in the piece and say "
                    "why it is weak. Name anything asserted without evidence. Say what an informed "
                    "sceptic would object to. Finish with the single most valuable change. Be "
                    "specific and be blunt — vague praise is worthless here.",
                    _COLUMN, 420, sub=True),
            ],
            "connections": [
                {"source": 0, "target": 1, "label": "1. draft it"},
                {"source": 0, "target": 2, "label": "2. cut it"},
                {"source": 0, "target": 3, "label": "3. challenge it"},
            ],
            "tools": [],
            "assignments": [],
        },
    },

    # ── parallel ────────────────────────────────────────────────────────────
    {
        "id": "due-diligence",
        "title": "Due Diligence Panel",
        "tagline": "Three independent opinions, gathered at once",
        "mode": "parallel",
        "what_it_shows": (
            "Parallel mode: every connected agent is asked the same question simultaneously and "
            "the lead reconciles the answers. Use it when the analyses are genuinely independent "
            "— a financial view does not need to wait for a technical one, and making it wait "
            "only makes the run slower."
        ),
        "look_for": (
            "Where the three disagree. Independent analysts given the same brief will weigh it "
            "differently, and the disagreement is more informative than any single verdict. The "
            "lead is instructed to surface conflicts rather than smooth them over."
        ),
        "needs_key": False,
        "optional_tools": ["tavily"],
        "sample_tasks": [
            "Should a 30-person B2B SaaS company build its own RAG system or buy one?",
            "Assess the risks of moving our primary database from Postgres to a managed service.",
            "Is it worth adopting a vector database when we have fewer than 10,000 documents?",
        ],
        "graph": {
            "agents": [
                _agent(
                    "Panel Chair",
                    "Puts the same question to all three, then reconciles",
                    "You chair a review panel. Put the question to all of your analysts, then write "
                    "the verdict. Your job is reconciliation, not summary: say where the analysts "
                    "agreed, and — more importantly — exactly where they disagreed and what the "
                    "disagreement turns on. If they conflict, say which view you find stronger and "
                    "why. Never average two opposing positions into a bland middle.",
                    *_LEAD, mode="parallel"),
                _agent(
                    "Financial Analyst",
                    "Cost, payback and what it displaces",
                    "You assess money. Total cost including the engineering time nobody counts, "
                    "realistic payback period, and what this spend displaces. Use the calculator "
                    "for arithmetic rather than estimating. State your assumptions as a list — "
                    "your numbers are only as good as those, and the panel needs to see them.",
                    _COLUMN, 120, sub=True),
                _agent(
                    "Technical Analyst",
                    "Feasibility, effort and what breaks",
                    "You assess build difficulty. What is genuinely hard here versus what merely "
                    "sounds hard, what the maintenance burden looks like a year in, and which part "
                    "will break first under load. Be concrete about effort. Distinguish between a "
                    "problem that is solved and a problem that is solved for your scale.",
                    _COLUMN, 270, sub=True),
                _agent(
                    "Market Analyst",
                    "Alternatives and the cost of waiting",
                    "You assess the outside world. What mature alternatives already exist, what "
                    "buying instead of building would cost in money and lock-in, and how fast this "
                    "area is moving. Say plainly whether waiting six months is likely to make this "
                    "decision easier or harder.",
                    _COLUMN, 420, sub=True),
            ],
            "connections": [
                {"source": 0, "target": 1, "label": "the money"},
                {"source": 0, "target": 2, "label": "the build"},
                {"source": 0, "target": 3, "label": "the market"},
            ],
            "tools": [
                {"name": "Calculator", "description": "Exact arithmetic evaluation",
                 "tool_type": "calculator", "is_builtin": True, "config": {}},
            ],
            "assignments": [{"agent": 1, "tool": 0}],
        },
    },

    # ── conditional ─────────────────────────────────────────────────────────
    {
        "id": "support-triage",
        "title": "Support Triage",
        "tagline": "Routes each ticket to one specialist, and only one",
        "mode": "conditional",
        "what_it_shows": (
            "Conditional mode: each connection carries a condition, and the lead routes to the "
            "agents whose conditions match. Unlike supervisor mode the routing rule is written "
            "down and inspectable, which is what you want when the same input must always go to "
            "the same place."
        ),
        "look_for": (
            "Run a billing question and then a technical one. Exactly one specialist wakes up each "
            "time, and the trace names the condition that matched — this is the difference between "
            "a routing rule you can audit and a model deciding on the day."
        ),
        "needs_key": False,
        "optional_tools": ["slack"],
        "sample_tasks": [
            "I was charged twice for my subscription this month and need a refund.",
            "The API returns 502 on every request since this morning. Nothing changed our end.",
            "This is the third time this has broken and I want to speak to someone senior.",
            "How do I export my data before cancelling?",
        ],
        "graph": {
            "agents": [
                _agent(
                    "Triage Desk",
                    "Reads the ticket and routes it by its conditions",
                    "You triage incoming support tickets. Read the ticket, decide which category it "
                    "belongs to, and route it to the matching specialist. Route to exactly one "
                    "unless the ticket genuinely spans two. Never answer the ticket yourself — your "
                    "value is accurate routing, and a wrong route costs more than a slow one. "
                    "Return the specialist's reply together with one line naming the category you "
                    "chose and why.",
                    *_LEAD, mode="conditional"),
                _agent(
                    "Billing Specialist",
                    "Charges, refunds, invoices, plan changes",
                    "You handle billing. Be precise about amounts, dates and what the customer will "
                    "actually see on their statement. State the refund timeline honestly rather "
                    "than optimistically. Never promise a specific refund date you cannot know — "
                    "give the range and say what it depends on.",
                    _COLUMN, 100, sub=True),
                _agent(
                    "Technical Support",
                    "Errors, outages, integration problems",
                    "You handle technical failures. Establish what changed and when before "
                    "suggesting anything. Ask for the specific error and the timestamp if they are "
                    "missing. Give a concrete next step rather than a list of possibilities, and "
                    "say clearly when something is a fault on our side rather than theirs.",
                    _COLUMN, 250, sub=True),
                _agent(
                    "Escalation Manager",
                    "Angry, repeated, or high-stakes tickets",
                    "You handle tickets that have gone wrong more than once, or where the customer "
                    "is losing patience. Acknowledge the specific failure rather than apologising "
                    "generically. Say what you are doing about it and by when. Do not defend the "
                    "company; the customer already knows something went wrong and wants it fixed.",
                    _COLUMN, 400, sub=True),
            ],
            "connections": [
                {"source": 0, "target": 1, "label": "billing",
                 "condition": "the ticket is about a charge, refund, invoice, subscription or plan change"},
                {"source": 0, "target": 2, "label": "technical",
                 "condition": "the ticket reports an error, outage, bug or integration failure"},
                {"source": 0, "target": 3, "label": "escalation",
                 "condition": "the customer is angry, the issue has recurred, or they ask for a manager"},
            ],
            "tools": [],
            "assignments": [],
        },
    },

    # ── a fifth, for the tool-heavy case ────────────────────────────────────
    {
        "id": "incident-review",
        "title": "Incident Review",
        "tagline": "Gathers the evidence, then writes the post-mortem",
        "mode": "sequential",
        "what_it_shows": (
            "The same sequential shape as the content pipeline, but with tools doing the work: "
            "one agent gathers evidence from outside sources, the next reconstructs what happened, "
            "the last writes it up. It shows that orchestration mode and tool use are independent "
            "choices — the mode decides who runs, the tools decide what they can find out."
        ),
        "look_for": (
            "That the timeline agent works only from what the evidence agent actually retrieved. "
            "Its prompt forbids filling gaps from memory, so an incomplete investigation produces "
            "a post-mortem with visible holes rather than a confident fiction."
        ),
        "needs_key": False,
        "optional_tools": ["github", "slack"],
        "sample_tasks": [
            "Write a post-mortem for an outage caused by an expired TLS certificate on the API gateway.",
            "Review an incident where a deploy took the checkout service down for 40 minutes.",
            "Reconstruct what happened: database connections exhausted after a traffic spike.",
        ],
        "graph": {
            "agents": [
                _agent(
                    "Incident Lead",
                    "Runs the review from evidence to write-up",
                    "You run incident reviews. Work in order: have the evidence gathered, have the "
                    "timeline reconstructed, then have it written up. Your final output is the "
                    "post-mortem itself. Keep it blameless — describe what the system allowed to "
                    "happen, never who made a mistake — and make sure every action item names an "
                    "owner and a deadline rather than an intention.",
                    *_LEAD, mode="sequential"),
                _agent(
                    "Evidence Gatherer",
                    "Collects what is actually known",
                    "You collect evidence and nothing else. Retrieve what you can about the "
                    "incident and report it as a list of facts, each with where it came from and "
                    "its timestamp. Explicitly list what you could not establish. You are not "
                    "allowed to explain the incident — that is someone else's job, and guessing "
                    "here poisons everything downstream.",
                    _COLUMN, 140, sub=True),
                _agent(
                    "Timeline Analyst",
                    "Reconstructs the sequence and the cause",
                    "You reconstruct what happened from the gathered evidence only. Build a "
                    "timestamped sequence, identify the trigger, and distinguish the trigger from "
                    "the underlying cause — they are rarely the same thing. Where the evidence does "
                    "not support a step, write 'unknown' rather than inferring it. A post-mortem "
                    "with honest gaps is worth more than a complete invented one.",
                    _COLUMN, 290, sub=True),
                _agent(
                    "Report Writer",
                    "Writes the post-mortem",
                    "You write the final document: summary, impact with real numbers where they "
                    "exist, timeline, root cause, what went well, what did not, and action items. "
                    "Each action item needs an owner and a deadline. Write for someone who was not "
                    "there and will read it in six months.",
                    _COLUMN, 440, sub=True),
            ],
            "connections": [
                {"source": 0, "target": 1, "label": "1. gather"},
                {"source": 0, "target": 2, "label": "2. reconstruct"},
                {"source": 0, "target": 3, "label": "3. write up"},
            ],
            "tools": [
                {"name": "Web Page Reader", "description": "Fetch a URL and read its text",
                 "tool_type": "web_fetch", "is_builtin": True, "config": {}},
                {"name": "Date & Time", "description": "Current date, time and day of week",
                 "tool_type": "datetime", "is_builtin": True, "config": {}},
            ],
            "assignments": [
                {"agent": 1, "tool": 0},
                {"agent": 2, "tool": 1},
            ],
        },
    },
]

EXAMPLES_BY_ID = {example["id"]: example for example in EXAMPLES}


def example_by_id(example_id):
    return EXAMPLES_BY_ID.get(example_id)


def summary(example):
    """The listing form: everything needed to choose an example, without the
    graph payload, which is large and only matters once one is picked."""
    graph = example["graph"]
    return {
        key: example[key] for key in
        ("id", "title", "tagline", "mode", "what_it_shows", "look_for",
         "needs_key", "optional_tools", "sample_tasks")
    } | {
        "agents": [
            {"name": agent["name"], "description": agent["description"],
             "is_lead": index == 0}
            for index, agent in enumerate(graph["agents"])
        ],
        "connections": len(graph["connections"]),
        "tools": [tool["name"] for tool in graph["tools"]],
    }
