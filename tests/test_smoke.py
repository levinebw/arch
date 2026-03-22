"""
End-to-end smoke tests for ARCH MCP server.

These tests start a REAL MCP server (uvicorn) and connect REAL MCP clients
via SSE. No mocks for the server/transport layer. This catches bugs that
unit tests miss because they mock create_subprocess_exec, MCP start/stop,
and all transport — the exact boundaries where UAT bugs live.

Requires: mcp >= 1.26.0 (sse_client + ClientSession)
"""

import asyncio
import json
import socket
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio

from mcp.client.sse import sse_client
from mcp import ClientSession

from arch.mcp_server import (
    MCPServer,
    WORKER_TOOLS,
    ARCHIE_ONLY_TOOLS,
    SYSTEM_TOOLS,
    GITHUB_TOOLS,
)
from arch.session import generate_mcp_config
from arch.state import StateStore


def get_free_port() -> int:
    """Find an available port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# -- Expected tool name sets --

WORKER_TOOL_NAMES = {t.name for t in WORKER_TOOLS}
ARCHIE_ONLY_TOOL_NAMES = {t.name for t in ARCHIE_ONLY_TOOLS}
SYSTEM_TOOL_NAMES = {t.name for t in SYSTEM_TOOLS}
GITHUB_TOOL_NAMES = {t.name for t in GITHUB_TOOLS}


# -- Fixtures --

@pytest_asyncio.fixture
async def smoke_server():
    """Start a real MCP server on a free port with a real StateStore."""
    port = get_free_port()

    with tempfile.TemporaryDirectory() as tmpdir:
        state = StateStore(Path(tmpdir) / "state")
        server = MCPServer(state=state, port=port)

        await server.start(background=True)
        # Give uvicorn a moment to fully bind
        await asyncio.sleep(0.3)

        yield server, port

        await server.stop()


@pytest_asyncio.fixture
async def smoke_server_with_github():
    """Start a real MCP server with github_repo set."""
    port = get_free_port()

    with tempfile.TemporaryDirectory() as tmpdir:
        state = StateStore(Path(tmpdir) / "state")
        server = MCPServer(
            state=state,
            port=port,
            github_repo="owner/repo",
        )

        await server.start(background=True)
        await asyncio.sleep(0.3)

        yield server, port

        await server.stop()


# -- Helper --

async def connect_as(port: int, agent_id: str):
    """Connect to the MCP server as a given agent. Returns (read, write, session)."""
    url = f"http://127.0.0.1:{port}/sse/{agent_id}"
    return sse_client(url)


# -- Tests --

class TestServerLifecycle:
    """Test that the MCP server starts, accepts connections, and stops cleanly."""

    @pytest.mark.asyncio
    async def test_server_starts_and_stops(self, smoke_server):
        """Server binds to port and a client can connect."""
        server, port = smoke_server

        async with sse_client(f"http://127.0.0.1:{port}/sse/test-agent") as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                # If we get here, the server accepted the SSE connection
                # and completed the MCP handshake.
                tools = await session.list_tools()
                assert len(tools.tools) > 0


class TestToolDiscovery:
    """Test that list_tools() returns correct tools per agent role."""

    @pytest.mark.asyncio
    async def test_worker_tool_discovery(self, smoke_server):
        """Worker agent sees worker tools + system tools, NOT Archie-only."""
        server, port = smoke_server

        async with sse_client(f"http://127.0.0.1:{port}/sse/frontend-1") as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
                tool_names = {t.name for t in result.tools}

                # Has all worker tools
                assert WORKER_TOOL_NAMES <= tool_names, (
                    f"Missing worker tools: {WORKER_TOOL_NAMES - tool_names}"
                )

                # Has system tools (needed for --permission-prompt-tool)
                assert SYSTEM_TOOL_NAMES <= tool_names, (
                    f"Missing system tools: {SYSTEM_TOOL_NAMES - tool_names}"
                )

                # Does NOT have Archie-only tools
                assert not (ARCHIE_ONLY_TOOL_NAMES & tool_names), (
                    f"Worker should not have Archie tools: {ARCHIE_ONLY_TOOL_NAMES & tool_names}"
                )

                # Does NOT have GitHub tools (no github_repo configured)
                assert not (GITHUB_TOOL_NAMES & tool_names), (
                    f"Worker should not have GitHub tools: {GITHUB_TOOL_NAMES & tool_names}"
                )

    @pytest.mark.asyncio
    async def test_archie_tool_discovery(self, smoke_server):
        """Archie sees worker + Archie-only + system tools (no GitHub without config)."""
        server, port = smoke_server

        async with sse_client(f"http://127.0.0.1:{port}/sse/archie") as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
                tool_names = {t.name for t in result.tools}

                # Has all worker tools
                assert WORKER_TOOL_NAMES <= tool_names

                # Has all Archie-only tools
                assert ARCHIE_ONLY_TOOL_NAMES <= tool_names, (
                    f"Missing Archie tools: {ARCHIE_ONLY_TOOL_NAMES - tool_names}"
                )

                # Has system tools
                assert SYSTEM_TOOL_NAMES <= tool_names

                # No GitHub tools (not configured)
                assert not (GITHUB_TOOL_NAMES & tool_names)

    @pytest.mark.asyncio
    async def test_archie_github_tools_conditional(self, smoke_server_with_github):
        """Archie sees GitHub tools when github_repo is configured."""
        server, port = smoke_server_with_github

        async with sse_client(f"http://127.0.0.1:{port}/sse/archie") as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
                tool_names = {t.name for t in result.tools}

                assert GITHUB_TOOL_NAMES <= tool_names, (
                    f"Missing GitHub tools: {GITHUB_TOOL_NAMES - tool_names}"
                )

    @pytest.mark.asyncio
    async def test_system_tools_discoverable(self, smoke_server):
        """handle_permission_request appears in list_tools() for both roles.

        This is the bug that caused UAT #2: SYSTEM_TOOLS were excluded from
        the tool catalog, so Claude CLI couldn't discover the permission
        prompt tool and Archie hung at 0 tokens.
        """
        server, port = smoke_server

        # Check worker
        async with sse_client(f"http://127.0.0.1:{port}/sse/worker-1") as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
                worker_names = {t.name for t in result.tools}

        # Check archie
        async with sse_client(f"http://127.0.0.1:{port}/sse/archie") as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.list_tools()
                archie_names = {t.name for t in result.tools}

        assert "handle_permission_request" in worker_names
        assert "handle_permission_request" in archie_names


class TestToolDispatch:
    """Test that tool calls route correctly through real MCP transport."""

    @pytest.mark.asyncio
    async def test_worker_send_message(self, smoke_server):
        """Worker can call send_message and get a valid response."""
        server, port = smoke_server

        async with sse_client(f"http://127.0.0.1:{port}/sse/frontend-1") as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                result = await session.call_tool(
                    "send_message",
                    {"to": "archie", "content": "Hello from smoke test"},
                )

                # Result is a list of TextContent
                assert len(result.content) == 1
                data = json.loads(result.content[0].text)
                assert "message_id" in data
                assert "timestamp" in data

        # Verify the message actually landed in state
        messages, last_id = server.state.get_messages("archie")
        assert len(messages) == 1
        assert messages[0]["content"] == "Hello from smoke test"
        assert messages[0]["from"] == "frontend-1"

    @pytest.mark.asyncio
    async def test_worker_update_status(self, smoke_server):
        """Worker can update its own status."""
        server, port = smoke_server

        # Register the agent first so update_status has something to update
        server.state.register_agent("backend-1", role="backend", worktree="/tmp/wt")

        async with sse_client(f"http://127.0.0.1:{port}/sse/backend-1") as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                result = await session.call_tool(
                    "update_status",
                    {"status": "working", "task": "Building API endpoints"},
                )

                data = json.loads(result.content[0].text)
                assert data.get("ok") is True

        # Verify state was updated
        agent = server.state.get_agent("backend-1")
        assert agent["status"] == "working"
        assert agent["task"] == "Building API endpoints"

    @pytest.mark.asyncio
    async def test_worker_access_denied_for_archie_tool(self, smoke_server):
        """Worker calling an Archie-only tool gets access denied."""
        server, port = smoke_server

        async with sse_client(f"http://127.0.0.1:{port}/sse/worker-1") as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                result = await session.call_tool(
                    "spawn_agent",
                    {"role": "hacker", "task": "break things"},
                )

                data = json.loads(result.content[0].text)
                assert "error" in data
                assert "Access denied" in data["error"]


class TestMultiAgent:
    """Test concurrent agent connections."""

    @pytest.mark.asyncio
    async def test_two_agents_concurrent(self, smoke_server):
        """Two agents connect simultaneously and get correct, isolated tool sets."""
        server, port = smoke_server

        async with sse_client(f"http://127.0.0.1:{port}/sse/archie") as (r1, w1):
            async with ClientSession(r1, w1) as archie_session:
                await archie_session.initialize()

                async with sse_client(f"http://127.0.0.1:{port}/sse/worker-1") as (r2, w2):
                    async with ClientSession(r2, w2) as worker_session:
                        await worker_session.initialize()

                        archie_tools = await archie_session.list_tools()
                        worker_tools = await worker_session.list_tools()

                        archie_names = {t.name for t in archie_tools.tools}
                        worker_names = {t.name for t in worker_tools.tools}

                        # Archie has spawn_agent, worker does not
                        assert "spawn_agent" in archie_names
                        assert "spawn_agent" not in worker_names

                        # Both have send_message
                        assert "send_message" in archie_names
                        assert "send_message" in worker_names

    @pytest.mark.asyncio
    async def test_agents_can_message_each_other(self, smoke_server):
        """Two agents exchange messages through the real MCP server."""
        server, port = smoke_server

        # Agent 1 sends a message
        async with sse_client(f"http://127.0.0.1:{port}/sse/frontend-1") as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                await session.call_tool(
                    "send_message",
                    {"to": "backend-1", "content": "Need the API ready"},
                )

        # Agent 2 reads messages
        async with sse_client(f"http://127.0.0.1:{port}/sse/backend-1") as (r, w):
            async with ClientSession(r, w) as session:
                await session.initialize()
                result = await session.call_tool("get_messages", {})

                data = json.loads(result.content[0].text)
                msgs = data["messages"]
                assert len(msgs) == 1
                assert msgs[0]["content"] == "Need the API ready"
                assert msgs[0]["from"] == "frontend-1"


class TestMCPConfig:
    """Test that generate_mcp_config produces URLs matching the server."""

    def test_mcp_config_matches_server(self, smoke_server):
        """generate_mcp_config() URL points to the right server endpoint."""
        server, port = smoke_server

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = generate_mcp_config(
                agent_id="test-agent",
                mcp_port=port,
                state_dir=Path(tmpdir),
                is_container=False,
            )

            config = json.loads(config_path.read_text())
            url = config["mcpServers"]["arch"]["url"]

            # URL should match the server's SSE endpoint format
            assert url == f"http://localhost:{port}/sse/test-agent"
            assert config["mcpServers"]["arch"]["type"] == "sse"

    def test_mcp_config_path_is_absolute(self):
        """MCP config path must be absolute so claude subprocess can find it
        regardless of its cwd (which is the agent's worktree, not the project root).

        This bug caused UAT #3: --mcp-config was a relative path, claude ran
        from .worktrees/archie/, couldn't find the config, never connected.
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            # Use a relative state_dir like arch.yaml's "./state"
            relative_dir = Path(tmpdir) / "state"
            relative_dir.mkdir()

            config_path = generate_mcp_config(
                agent_id="test-agent",
                mcp_port=3999,
                state_dir=relative_dir,
            )

            # The returned path must be absolute
            assert config_path.is_absolute(), \
                f"MCP config path must be absolute, got: {config_path}"

    def test_mcp_config_container_uses_docker_host(self):
        """Container config uses host.docker.internal instead of localhost."""
        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = generate_mcp_config(
                agent_id="container-agent",
                mcp_port=4000,
                state_dir=Path(tmpdir),
                is_container=True,
            )

            config = json.loads(config_path.read_text())
            url = config["mcpServers"]["arch"]["url"]

            assert "host.docker.internal" in url
            assert "localhost" not in url


class TestSkillsSmoke:
    """Smoke tests for skills system via real MCP transport."""

    @pytest.mark.asyncio
    async def test_list_personas_includes_skills(self):
        """list_personas returns skills for directory personas via real MCP."""
        port = get_free_port()

        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir) / "repo"
            # Create a directory persona with a skill
            eng_dir = repo / "personas" / "engineering"
            skill_dir = eng_dir / "skills" / "deploy"
            skill_dir.mkdir(parents=True)
            (eng_dir / "CLAUDE.md").write_text("# Engineering\nBuilds things.")
            (skill_dir / "SKILL.md").write_text(
                "---\nname: deploy\ndescription: Deploy to production\n---\nSteps."
            )
            # Create a flat persona (no skills)
            (repo / "personas" / "qa.md").write_text("# QA\nTests things.")

            state = StateStore(Path(tmpdir) / "state")
            server = MCPServer(state=state, port=port, repo_path=repo)

            await server.start(background=True)
            await asyncio.sleep(0.3)

            try:
                async with sse_client(f"http://127.0.0.1:{port}/sse/archie") as (r, w):
                    async with ClientSession(r, w) as session:
                        await session.initialize()

                        result = await session.call_tool("list_personas", {})
                        data = json.loads(result.content[0].text)

                        personas = {p["name"]: p for p in data["personas"]}

                        # Engineering has skills
                        assert "engineering" in personas
                        assert len(personas["engineering"]["skills"]) == 1
                        assert personas["engineering"]["skills"][0]["name"] == "deploy"
                        assert personas["engineering"]["skills"][0]["description"] == "Deploy to production"

                        # QA has no skills
                        assert "qa" in personas
                        assert personas["qa"]["skills"] == []
            finally:
                await server.stop()

    @pytest.mark.asyncio
    async def test_get_skill_via_mcp(self):
        """get_skill returns full SKILL.md content via real MCP transport."""
        port = get_free_port()

        with tempfile.TemporaryDirectory() as tmpdir:
            repo = Path(tmpdir) / "repo"
            skill_dir = repo / "personas" / "engineering" / "skills" / "deploy"
            skill_dir.mkdir(parents=True)
            (repo / "personas" / "engineering" / "CLAUDE.md").write_text("# Eng\n")
            (skill_dir / "SKILL.md").write_text(
                "---\nname: deploy\ndescription: Deploy to prod\n---\n\n"
                "## Process\n1. Run tests\n2. Push to prod\n\n"
                "## Quality Criteria\n- No downtime\n"
            )

            state = StateStore(Path(tmpdir) / "state")
            server = MCPServer(state=state, port=port, repo_path=repo)

            await server.start(background=True)
            await asyncio.sleep(0.3)

            try:
                async with sse_client(f"http://127.0.0.1:{port}/sse/archie") as (r, w):
                    async with ClientSession(r, w) as session:
                        await session.initialize()

                        result = await session.call_tool("get_skill", {
                            "persona": "engineering",
                            "skill": "deploy",
                        })
                        data = json.loads(result.content[0].text)

                        assert data["persona"] == "engineering"
                        assert data["skill"] == "deploy"
                        assert "## Process" in data["content"]
                        assert "## Quality Criteria" in data["content"]
                        assert "No downtime" in data["content"]
            finally:
                await server.stop()

    @pytest.mark.asyncio
    async def test_get_skill_access_denied_for_worker(self):
        """Workers cannot call get_skill — it's Archie-only."""
        port = get_free_port()

        with tempfile.TemporaryDirectory() as tmpdir:
            state = StateStore(Path(tmpdir) / "state")
            server = MCPServer(state=state, port=port)

            await server.start(background=True)
            await asyncio.sleep(0.3)

            try:
                async with sse_client(f"http://127.0.0.1:{port}/sse/worker-1") as (r, w):
                    async with ClientSession(r, w) as session:
                        await session.initialize()

                        result = await session.call_tool("get_skill", {
                            "persona": "engineering",
                            "skill": "deploy",
                        })
                        data = json.loads(result.content[0].text)
                        assert "error" in data
                        assert "Access denied" in data["error"]
            finally:
                await server.stop()
