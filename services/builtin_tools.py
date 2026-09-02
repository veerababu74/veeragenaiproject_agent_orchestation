"""Built-in tool implementations.

Every tool here is built from a plain REST call with aiohttp rather than a
provider SDK, so adding a tool never adds a dependency. Each factory returns a
StructuredTool (or None when required config is missing) and every network call
is wrapped so a failing tool returns a message the agent can reason about
instead of raising through the graph.
"""

import ast
import json
import operator
from datetime import datetime, timedelta, timezone
from typing import Optional

import aiohttp
from langchain_core.tools import StructuredTool
from pydantic import BaseModel, Field

TIMEOUT = aiohttp.ClientTimeout(total=30)


async def _request(method, url, **kwargs):
    """One HTTP round trip, with errors returned as text rather than raised."""
    try:
        async with aiohttp.ClientSession(timeout=TIMEOUT) as session:
            async with session.request(method, url, **kwargs) as response:
                body = await response.text()
                if response.status >= 400:
                    return None, f'HTTP {response.status}: {body[:400]}'
                try:
                    return json.loads(body), None
                except ValueError:
                    return body, None
    except Exception as error:
        return None, f'Request failed: {error}'


# ── Date and time ───────────────────────────────────────────────────────────

class DatetimeInput(BaseModel):
    # Optional rather than 0 so that omitting it falls through to the offset the
    # user configured on the tool, instead of silently resetting it to UTC.
    timezone_offset_hours: Optional[float] = Field(
        default=None,
        description='Offset from UTC in hours, e.g. 5.5 for IST, -5 for EST. Omit to use the configured default.',
    )
    days_offset: int = Field(
        default=0,
        description='Shift the result by this many days: 1 for tomorrow, -1 for yesterday.',
    )


def create_datetime_tool(config):
    default_offset = float(config.get('timezone_offset_hours') or 0)

    async def _run(timezone_offset_hours: float = None, days_offset: int = 0):
        offset = default_offset if timezone_offset_hours is None else timezone_offset_hours
        moment = datetime.now(timezone(timedelta(hours=offset))) + timedelta(days=days_offset)
        return json.dumps({
            'iso': moment.isoformat(),
            'date': moment.strftime('%Y-%m-%d'),
            'time': moment.strftime('%H:%M:%S'),
            'day_of_week': moment.strftime('%A'),
            'utc_offset_hours': offset,
        })

    return StructuredTool.from_function(
        coroutine=_run, name='current_datetime', args_schema=DatetimeInput,
        description='Get the current date, time and day of week. Use this whenever the answer depends on what today is.',
    )


# ── Calculator ──────────────────────────────────────────────────────────────

_OPERATORS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.FloorDiv: operator.floordiv, ast.Mod: operator.mod,
    ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos,
}


def _eval_node(node):
    """Evaluate an arithmetic AST. Only numeric literals and operators in
    _OPERATORS are reachable, so no name lookup or call can be expressed."""
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError('Only numbers are allowed')
    if isinstance(node, ast.BinOp) and type(node.op) in _OPERATORS:
        return _OPERATORS[type(node.op)](_eval_node(node.left), _eval_node(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPERATORS:
        return _OPERATORS[type(node.op)](_eval_node(node.operand))
    raise ValueError('Unsupported expression')


class CalculatorInput(BaseModel):
    expression: str = Field(description='Arithmetic expression, e.g. "(1200 * 0.18) + 45"')


def create_calculator_tool(_config):
    async def _run(expression: str):
        try:
            if len(expression) > 200:
                return 'Expression is too long.'
            return str(_eval_node(ast.parse(expression, mode='eval').body))
        except Exception as error:
            return f'Could not evaluate "{expression}": {error}'

    return StructuredTool.from_function(
        coroutine=_run, name='calculator', args_schema=CalculatorInput,
        description='Evaluate an arithmetic expression exactly. Use for any calculation instead of doing mental math.',
    )


# ── Slack ───────────────────────────────────────────────────────────────────

class SlackInput(BaseModel):
    message: str = Field(description='The message text to post to Slack')
    channel: str = Field(default='', description='Channel to post to, e.g. "#general". Only used with a bot token.')


def create_slack_tool(config):
    webhook_url = (config.get('webhook_url') or '').strip()
    bot_token = (config.get('api_key') or config.get('bot_token') or '').strip()
    default_channel = (config.get('channel') or '').strip()
    if not webhook_url and not bot_token:
        return None

    async def _run(message: str, channel: str = ''):
        target = (channel or default_channel).strip()
        if webhook_url:
            payload = {'text': message}
            if target:
                payload['channel'] = target
            _, error = await _request('POST', webhook_url, json=payload)
            return error or f'Message posted to Slack{f" ({target})" if target else ""}.'
        if not target:
            return 'A channel is required when posting with a bot token.'
        data, error = await _request(
            'POST', 'https://slack.com/api/chat.postMessage',
            headers={'Authorization': f'Bearer {bot_token}', 'Content-Type': 'application/json'},
            json={'channel': target, 'text': message},
        )
        if error:
            return error
        if isinstance(data, dict) and not data.get('ok'):
            return f"Slack rejected the message: {data.get('error', 'unknown error')}"
        return f'Message posted to {target}.'

    return StructuredTool.from_function(
        coroutine=_run, name='slack_post_message', args_schema=SlackInput,
        description='Post a message to Slack. Use when the user asks to notify, send or share something on Slack.',
    )


# ── Web search ──────────────────────────────────────────────────────────────

class SearchInput(BaseModel):
    query: str = Field(description='The search query')


def create_tavily_tool(config):
    api_key = (config.get('api_key') or '').strip()
    if not api_key:
        return None
    max_results = int(config.get('max_results') or 5)

    async def _run(query: str):
        data, error = await _request(
            'POST', 'https://api.tavily.com/search',
            json={'api_key': api_key, 'query': query, 'max_results': max_results, 'search_depth': 'basic'},
        )
        if error:
            return f'Tavily search failed: {error}'
        results = (data or {}).get('results', []) if isinstance(data, dict) else []
        if not results:
            return 'No results found.'
        answer = (data or {}).get('answer')
        lines = [f"[{r.get('title', '')}]({r.get('url', '')})\n{r.get('content', '')[:500]}" for r in results]
        return (f'{answer}\n\n' if answer else '') + '\n\n'.join(lines)

    return StructuredTool.from_function(
        coroutine=_run, name='tavily_search', args_schema=SearchInput,
        description='Search the web with Tavily for current information. Use for recent events, prices, news or anything you are unsure about.',
    )


def create_serper_tool(config):
    api_key = (config.get('api_key') or '').strip()
    if not api_key:
        return None
    max_results = int(config.get('max_results') or 5)

    async def _run(query: str):
        data, error = await _request(
            'POST', 'https://google.serper.dev/search',
            headers={'X-API-KEY': api_key, 'Content-Type': 'application/json'},
            json={'q': query, 'num': max_results},
        )
        if error:
            return f'Google search failed: {error}'
        organic = (data or {}).get('organic', []) if isinstance(data, dict) else []
        if not organic:
            return 'No results found.'
        lines = [f"[{r.get('title', '')}]({r.get('link', '')})\n{r.get('snippet', '')}" for r in organic[:max_results]]
        return '\n\n'.join(lines)

    return StructuredTool.from_function(
        coroutine=_run, name='google_search', args_schema=SearchInput,
        description='Search Google (via Serper) for current information on the web.',
    )


# ── HTTP and web pages ──────────────────────────────────────────────────────

class WebFetchInput(BaseModel):
    url: str = Field(description='The full URL of the page to read, including https://')


def create_web_fetch_tool(_config):
    async def _run(url: str):
        if not url.startswith(('http://', 'https://')):
            return 'URL must start with http:// or https://'
        body, error = await _request('GET', url, headers={'User-Agent': 'Mozilla/5.0 (AgentOrchestrator)'})
        if error:
            return f'Could not fetch the page: {error}'
        if isinstance(body, (dict, list)):
            return json.dumps(body)[:6000]
        import re
        text = re.sub(r'<(script|style)[^>]*>.*?</\1>', ' ', body or '', flags=re.S | re.I)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text[:6000] or 'The page returned no readable text.'

    return StructuredTool.from_function(
        coroutine=_run, name='fetch_web_page', args_schema=WebFetchInput,
        description='Fetch a URL and return its readable text. Use to read an article or page the user linked.',
    )


class HttpRequestInput(BaseModel):
    url: str = Field(description='Full URL to call, including https://')
    method: str = Field(default='GET', description='HTTP method: GET, POST, PUT, PATCH or DELETE')
    body: dict = Field(default={}, description='JSON body to send for POST/PUT/PATCH')


def create_http_tool(config):
    extra_headers = config.get('headers') if isinstance(config.get('headers'), dict) else {}

    async def _run(url: str, method: str = 'GET', body: dict = None):
        if not url.startswith(('http://', 'https://')):
            return 'URL must start with http:// or https://'
        method = (method or 'GET').upper()
        if method not in {'GET', 'POST', 'PUT', 'PATCH', 'DELETE'}:
            return f'Unsupported method: {method}'
        kwargs = {'headers': {'Content-Type': 'application/json', **extra_headers}}
        if method in {'POST', 'PUT', 'PATCH'}:
            kwargs['json'] = body or {}
        data, error = await _request(method, url, **kwargs)
        if error:
            return error
        return json.dumps(data)[:6000] if isinstance(data, (dict, list)) else str(data)[:6000]

    return StructuredTool.from_function(
        coroutine=_run, name='http_request', args_schema=HttpRequestInput,
        description='Call any HTTP JSON API. Use when the user asks to hit an endpoint that no other tool covers.',
    )


# ── GitHub ──────────────────────────────────────────────────────────────────

class GithubInput(BaseModel):
    action: str = Field(description='One of: list_issues, get_issue, create_issue, list_commits, get_repo')
    title: str = Field(default='', description='Issue title, for create_issue')
    body: str = Field(default='', description='Issue body, for create_issue')
    issue_number: int = Field(default=0, description='Issue number, for get_issue')


def create_github_tool(config):
    token = (config.get('api_key') or '').strip()
    repo = (config.get('repo') or '').strip()
    if not token or not repo:
        return None
    headers = {'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json'}
    base = f'https://api.github.com/repos/{repo}'

    async def _run(action: str, title: str = '', body: str = '', issue_number: int = 0):
        if action == 'list_issues':
            data, error = await _request('GET', f'{base}/issues?per_page=20', headers=headers)
            if error:
                return error
            return json.dumps([{'number': i.get('number'), 'title': i.get('title'), 'state': i.get('state')}
                               for i in data or [] if isinstance(i, dict)])
        if action == 'get_issue':
            data, error = await _request('GET', f'{base}/issues/{issue_number}', headers=headers)
            return error or json.dumps({k: data.get(k) for k in ('number', 'title', 'state', 'body')})
        if action == 'create_issue':
            if not title:
                return 'A title is required to create an issue.'
            data, error = await _request('POST', f'{base}/issues', headers=headers, json={'title': title, 'body': body})
            return error or f"Created issue #{data.get('number')}: {data.get('html_url')}"
        if action == 'list_commits':
            data, error = await _request('GET', f'{base}/commits?per_page=20', headers=headers)
            if error:
                return error
            return json.dumps([{'sha': (c.get('sha') or '')[:8], 'message': (c.get('commit') or {}).get('message', '')}
                               for c in data or [] if isinstance(c, dict)])
        if action == 'get_repo':
            data, error = await _request('GET', base, headers=headers)
            return error or json.dumps({k: data.get(k) for k in ('full_name', 'description', 'stargazers_count', 'open_issues_count')})
        return f'Unknown action: {action}'

    return StructuredTool.from_function(
        coroutine=_run, name='github', args_schema=GithubInput,
        description=f'Read and write issues, commits and metadata on the GitHub repository {repo}.',
    )


# ── Registry ────────────────────────────────────────────────────────────────

# Each entry describes the tool for the /tools/builtin catalogue the UI renders,
# and points at the factory the orchestrator calls. `config_fields` are the
# inputs the user fills in when adding the tool; `requires` lists the ones
# without which the factory returns None.
BUILTIN_TOOLS = {
    'datetime': {
        'name': 'Date & Time', 'description': 'Current date, time and day of week',
        'config_fields': ['timezone_offset_hours'], 'requires': [], 'factory': create_datetime_tool,
    },
    'calculator': {
        'name': 'Calculator', 'description': 'Exact arithmetic evaluation',
        'config_fields': [], 'requires': [], 'factory': create_calculator_tool,
    },
    'slack': {
        'name': 'Slack', 'description': 'Post messages to a Slack channel',
        'config_fields': ['webhook_url', 'api_key', 'channel'], 'requires': ['webhook_url|api_key'],
        'factory': create_slack_tool,
    },
    'tavily': {
        'name': 'Tavily Search', 'description': 'AI-optimized web search',
        'config_fields': ['api_key', 'max_results'], 'requires': ['api_key'], 'factory': create_tavily_tool,
    },
    'google_search': {
        'name': 'Google Search (Serper)', 'description': 'Google results via the Serper API',
        'config_fields': ['api_key', 'max_results'], 'requires': ['api_key'], 'factory': create_serper_tool,
    },
    'web_fetch': {
        'name': 'Web Page Reader', 'description': 'Fetch a URL and read its text',
        'config_fields': [], 'requires': [], 'factory': create_web_fetch_tool,
    },
    'http_request': {
        'name': 'HTTP Request', 'description': 'Call any JSON API endpoint',
        'config_fields': ['headers'], 'requires': [], 'factory': create_http_tool,
    },
    'github': {
        'name': 'GitHub', 'description': 'Issues, commits and repo metadata',
        'config_fields': ['api_key', 'repo'], 'requires': ['api_key', 'repo'], 'factory': create_github_tool,
    },
}


def create_builtin(tool_type, config):
    entry = BUILTIN_TOOLS.get(tool_type)
    if not entry:
        return None
    try:
        return entry['factory'](config or {})
    except Exception:
        return None
