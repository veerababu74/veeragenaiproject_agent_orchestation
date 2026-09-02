import sqlite3, json, time, os
from pathlib import Path

from config import settings

DB_PATH = settings.sqlite_path("agent_data.db", Path(__file__).parent)

def get_db():
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS agents (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL, description TEXT DEFAULT '',
            system_prompt TEXT DEFAULT '', llm_provider TEXT DEFAULT 'openai', llm_model TEXT DEFAULT 'gpt-4o',
            temperature REAL DEFAULT 0.7, max_tokens INTEGER DEFAULT 4096, is_sub_agent INTEGER DEFAULT 0,
            parent_id TEXT, position_x REAL DEFAULT 0, position_y REAL DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now')), updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS agent_connections (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL, source_agent_id TEXT NOT NULL, target_agent_id TEXT NOT NULL,
            label TEXT DEFAULT '', condition_expr TEXT DEFAULT '', created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (source_agent_id) REFERENCES agents(id) ON DELETE CASCADE,
            FOREIGN KEY (target_agent_id) REFERENCES agents(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS tools (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL, description TEXT DEFAULT '',
            tool_type TEXT DEFAULT 'custom', is_builtin INTEGER DEFAULT 0, config TEXT DEFAULT '{}',
            created_at TEXT DEFAULT (datetime('now')), updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS tool_assignments (
            id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, tool_id TEXT NOT NULL, created_at TEXT DEFAULT (datetime('now')),
            UNIQUE(agent_id, tool_id),
            FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE,
            FOREIGN KEY (tool_id) REFERENCES tools(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS custom_tool_schemas (
            id TEXT PRIMARY KEY, tool_id TEXT UNIQUE NOT NULL, api_url TEXT NOT NULL, method TEXT DEFAULT 'POST',
            headers TEXT DEFAULT '{}', request_body TEXT DEFAULT '{}', response_body TEXT DEFAULT '{}',
            path_params TEXT DEFAULT '[]', query_params TEXT DEFAULT '[]', auth_type TEXT DEFAULT 'none',
            auth_config TEXT DEFAULT '{}', created_at TEXT DEFAULT (datetime('now')), updated_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (tool_id) REFERENCES tools(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS rag_documents (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL, file_name TEXT NOT NULL, file_type TEXT NOT NULL,
            file_size INTEGER DEFAULT 0, remote_path TEXT DEFAULT '', embedding_model TEXT DEFAULT '',
            chunk_count INTEGER DEFAULT 0, status TEXT DEFAULT 'pending', error_message TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')), expires_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS llm_configs (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL, provider TEXT NOT NULL, api_key TEXT NOT NULL,
            base_url TEXT DEFAULT '', models TEXT DEFAULT '[]', is_active INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now')), updated_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS agent_executions (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL, agent_id TEXT NOT NULL, input_text TEXT DEFAULT '',
            output_text TEXT DEFAULT '', status TEXT DEFAULT 'pending', error_message TEXT DEFAULT '',
            duration_ms INTEGER DEFAULT 0, tokens_used INTEGER DEFAULT 0, created_at TEXT DEFAULT (datetime('now')),
            FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS conversation_messages (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
            agent_id TEXT NOT NULL, role TEXT NOT NULL, content TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_convmsg ON conversation_messages(user_id, conversation_id, created_at);
        CREATE INDEX IF NOT EXISTS idx_agents_user ON agents(user_id);
        CREATE INDEX IF NOT EXISTS idx_connections_user ON agent_connections(user_id);
        CREATE INDEX IF NOT EXISTS idx_tools_user ON tools(user_id);
        CREATE INDEX IF NOT EXISTS idx_rag_user ON rag_documents(user_id);
        CREATE INDEX IF NOT EXISTS idx_llm_user ON llm_configs(user_id);
        CREATE INDEX IF NOT EXISTS idx_exec_user ON agent_executions(user_id);
    """)
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(rag_documents)")}
    if "remote_path" not in columns:
        conn.execute("ALTER TABLE rag_documents ADD COLUMN remote_path TEXT DEFAULT ''")
    if "embedding_model" not in columns:
        conn.execute("ALTER TABLE rag_documents ADD COLUMN embedding_model TEXT DEFAULT ''")
    conn.commit()
    conn.close()
    print("Database initialized.")

def cleanup_expired_data():
    conn = get_db()
    expiring_docs = [dict(r) for r in conn.execute(
        "SELECT id, user_id, chunk_count, remote_path, status FROM rag_documents WHERE created_at < datetime('now', '-48 hours')"
    ).fetchall()]
    conn.close()

    if expiring_docs:
        import rag_storage
        import rag_vector_store
        for doc in expiring_docs:
            if doc.get('status') == 'ready':
                try:
                    rag_vector_store.delete_document(doc['user_id'], doc['id'], doc.get('chunk_count') or 0)
                except Exception:
                    pass
                if doc.get('remote_path'):
                    try:
                        rag_storage.delete(doc['remote_path'])
                    except Exception:
                        pass

    conn = get_db()
    conn.execute("DELETE FROM conversation_messages WHERE created_at < datetime('now', '-48 hours')")
    conn.execute("DELETE FROM agent_executions WHERE created_at < datetime('now', '-48 hours')")
    conn.execute("DELETE FROM rag_documents WHERE created_at < datetime('now', '-48 hours')")
    conn.execute("DELETE FROM tool_assignments WHERE created_at < datetime('now', '-48 hours')")
    conn.execute("DELETE FROM custom_tool_schemas WHERE created_at < datetime('now', '-48 hours')")
    conn.execute("DELETE FROM tools WHERE created_at < datetime('now', '-48 hours')")
    conn.execute("DELETE FROM agent_connections WHERE created_at < datetime('now', '-48 hours')")
    conn.execute("DELETE FROM agents WHERE created_at < datetime('now', '-48 hours')")
    conn.execute("DELETE FROM llm_configs WHERE created_at < datetime('now', '-48 hours')")
    conn.commit()
    conn.close()
