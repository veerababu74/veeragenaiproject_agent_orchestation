from pydantic import BaseModel, Field
from typing import Optional, Any
from enum import Enum

class AgentCreate(BaseModel):
    name: str
    description: str = ""
    system_prompt: str = ""
    llm_provider: str = "openai"
    llm_model: str = "gpt-4o"
    temperature: float = 0.7
    max_tokens: int = 4096
    is_sub_agent: bool = False
    parent_id: Optional[str] = None
    position_x: float = 0
    position_y: float = 0
    # Optional key for llm_provider, saved to the user's provider keys rather
    # than onto the agent row, so agents sharing a provider share one key.
    api_key: Optional[str] = None
    base_url: Optional[str] = None

class AgentUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    system_prompt: Optional[str] = None
    llm_provider: Optional[str] = None
    llm_model: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    is_sub_agent: Optional[bool] = None
    parent_id: Optional[str] = None
    position_x: Optional[float] = None
    position_y: Optional[float] = None
    api_key: Optional[str] = None
    base_url: Optional[str] = None

class ConnectionCreate(BaseModel):
    source_agent_id: str
    target_agent_id: str
    label: str = ""
    condition: str = ""

class ToolCreate(BaseModel):
    name: str
    description: str = ""
    tool_type: str = "custom"
    is_builtin: bool = False
    config: dict = {}

class ToolUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    tool_type: Optional[str] = None
    config: Optional[dict] = None

class CustomToolSchemaCreate(BaseModel):
    api_url: str
    method: str = "POST"
    headers: dict = {}
    request_body: dict = {}
    response_body: dict = {}
    path_params: list = []
    query_params: list = []
    auth_type: str = "none"
    auth_config: dict = {}

class ToolAssignmentCreate(BaseModel):
    agent_id: str
    tool_id: str

class LlmConfigCreate(BaseModel):
    provider: str
    api_key: str
    base_url: str = ""
    models: list = []

class ExecuteRequest(BaseModel):
    agent_id: str
    message: str
    stream: bool = False
    conversation_id: Optional[str] = None
