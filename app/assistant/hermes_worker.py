"""Runs ONE Hermes agent turn in a throwaway process.

Executed by app/assistant/service.py with the interpreter of the isolated
Hermes venv (see requirements-hermes.txt), so this module must only import
the standard library, PyYAML and Hermes itself - never the rest of the app
(except the dependency-free `prompts` module).

Everything arrives via environment variables (not argv, which `ps` shows):
ASSISTANT_QUESTION, ASSISTANT_MCP_URL, ASSISTANT_SCOPE_TOKEN, ASSISTANT_RUN_ID,
ASSISTANT_HERMES_MODEL, OPENROUTER_API_KEY, optional ASSISTANT_HERMES_HOME /
ASSISTANT_MAX_ITERATIONS / ASSISTANT_MAX_OUTPUT_TOKENS / OPENROUTER_BASE_URL.

stdout carries exactly one JSON document; everything else goes to stderr.

Verified against hermes-agent 0.19.0 (pip install "hermes-agent[mcp]"):
  * MCP servers are read from $HERMES_HOME/config.yaml (`mcp_servers`), with
    `url` + `headers` for HTTP transport and ${ENV_VAR} interpolation.
  * Library mode does NOT discover MCP tools by itself - discover_mcp_tools()
    must be called before AIAgent() or the agent silently has zero tools.
  * Without the `mcp` extra Hermes disables MCP entirely (no error).
  * An MCP server named `chat_scope` is exposed as toolset `chat_scope`
    (alias of `mcp-chat_scope`); its tools are named mcp__chat_scope__<tool>.
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

import yaml

from app.assistant.prompts import SYSTEM_PROMPT

MCP_SERVER_NAME = "chat_scope"
ALLOWED_TOOLS = [
    "get_recent_chat_context",
    "search_chat_messages",
    "search_chat_documents",
    "get_document_content",
]


def _write_config(home: Path, mcp_url: str) -> None:
    config = {
        "mcp_servers": {
            MCP_SERVER_NAME: {
                "url": mcp_url,
                # Resolved by Hermes from the process environment, so the
                # token itself is never written to disk.
                "headers": {"Authorization": "Bearer ${ASSISTANT_SCOPE_TOKEN}"},
                "enabled": True,
                "timeout": 60,
                "connect_timeout": 15,
                "tools": {"include": ALLOWED_TOOLS, "resources": False, "prompts": False},
            }
        }
    }
    path = home / "config.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    path.chmod(0o600)


def _run(home: Path) -> dict:
    question = os.environ["ASSISTANT_QUESTION"]
    _write_config(home, os.environ["ASSISTANT_MCP_URL"])
    os.environ["HERMES_HOME"] = str(home)  # must be set before importing hermes

    from tools.mcp_tool import discover_mcp_tools

    discovered = discover_mcp_tools()
    if not discovered:
        # Fail closed: answering without tools would silently ignore the chat
        # and documents the user is asking about.
        raise RuntimeError(
            "no MCP tools discovered (is hermes-agent[mcp] installed and the "
            "/mcp endpoint reachable with the scope token?)"
        )

    from run_agent import AIAgent

    agent = AIAgent(
        model=os.environ["ASSISTANT_HERMES_MODEL"],
        api_key=os.environ["OPENROUTER_API_KEY"],
        base_url=os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"),
        quiet_mode=True,
        # Whitelist: only our MCP server's tools. The denylist is belt and braces.
        enabled_toolsets=[MCP_SERVER_NAME],
        disabled_toolsets=["terminal", "browser", "computer", "vision"],
        max_iterations=int(os.getenv("ASSISTANT_MAX_ITERATIONS", "12")),
        max_tokens=int(os.getenv("ASSISTANT_MAX_OUTPUT_TOKENS", "1200")),
        ephemeral_system_prompt=SYSTEM_PROMPT,
        skip_memory=True,
        skip_context_files=True,
        save_trajectories=False,
    )
    result = agent.run_conversation(
        user_message=question,
        task_id=os.getenv("ASSISTANT_RUN_ID"),
    )
    return {
        "final_response": result.get("final_response", ""),
        "messages": result.get("messages", []),
    }


def main() -> int:
    # The service creates and removes this directory, so it is cleaned up even
    # when the run is killed on timeout/cancel; the fallback is for manual runs.
    external_home = os.getenv("ASSISTANT_HERMES_HOME")
    home = Path(external_home) if external_home else Path(tempfile.mkdtemp(prefix="hermes-assistant-"))
    try:
        # Libraries (and Hermes itself) may print; keep stdout for our JSON only.
        with contextlib.redirect_stdout(sys.stderr):
            output = _run(home)
        print(json.dumps(output, ensure_ascii=False, default=str))
        return 0
    finally:
        if not external_home:
            shutil.rmtree(home, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
