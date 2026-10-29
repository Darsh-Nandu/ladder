# Ladder as an MCP server

`app/mcp_server.py` exposes Ladder over the [Model Context Protocol](https://modelcontextprotocol.io), so a
coding assistant (Claude Code, Cursor, Windsurf, ...) can hand off "write a function that passes these tests"
to the Nemotron ladder and get back verified code plus what it cost.

```bash
.venv/bin/pip install -r requirements-mcp.txt     # MCP Python SDK v2 (mcp>=2.3)
.venv/bin/python -m app.mcp_server                 # stdio (what most clients launch)
.venv/bin/python -m app.mcp_server --http --port 8765   # streamable HTTP at http://127.0.0.1:8765/mcp
```

The server needs `NEBIUS_API_KEY` (from the environment, or the project's `.env`). Without it, tools still
list fine; `solve_function` returns `status: "error"`.

## Register it

### Claude Code

```bash
cd /path/to/ladder
claude mcp add ladder -e NEBIUS_API_KEY=your-key -e PYTHONPATH="$PWD" -- "$PWD/.venv/bin/python" -m app.mcp_server
```

`PYTHONPATH` matters: Claude Code starts the server from *your* project directory, and `-m app.mcp_server`
must still find Ladder's `app` package. Check with `claude mcp list`. Solves can take minutes, so raise the
tool timeout if needed (`MCP_TOOL_TIMEOUT=600000 claude`).

### Cursor and other JSON-configured clients

`.cursor/mcp.json` (Cursor), `claude_desktop_config.json` (Claude Desktop), and most others use this shape:

```json
{
  "mcpServers": {
    "ladder": {
      "command": "/path/to/ladder/.venv/bin/python",
      "args": ["-m", "app.mcp_server"],
      "cwd": "/path/to/ladder",
      "env": { "NEBIUS_API_KEY": "your-key", "PYTHONPATH": "/path/to/ladder" }
    }
  }
}
```

For HTTP clients, run with `--http` and point them at `http://127.0.0.1:8765/mcp`.

**HTTP mode has no authentication.** Anyone who can reach the port can make the server run LLM-generated code in
its sandbox and spend your `NEBIUS_API_KEY` credits. Keep it on loopback (the default; the SDK then also enables
DNS-rebinding protection). The server refuses a non-loopback `--host` unless you also pass `--allow-remote`,
which you should only do behind your own authenticating proxy or firewall.

## Tools

### `solve_function(spec, tests="", starter="", policy="ladder-wide")`

| arg | meaning |
|---|---|
| `spec` | what to build: function name, signature, behaviour, examples (max 8000 chars) |
| `tests` | optional test functions with plain `assert`; the function is already in scope, so don't import it. Never modified. Empty = Ladder generates tests from the spec (max 12000 chars) |
| `starter` | optional signature / starter code (max 8000 chars) |
| `policy` | one of `list_policies()`; an unknown name is rejected with the list of valid ones |

Returns structured content (plus the same as JSON text):

```json
{
  "status": "solved",
  "solved_by": "super",
  "attempts": 7,
  "cost_usd": 0.0021,
  "ultra_equivalent_cost_usd": 0.0094,
  "wall_seconds": 41.2,
  "code": "def add(a, b):\n    return a + b",
  "tests": "def test_small():\n    assert add(1, 2) == 3\n...",
  "summary": "nano ✗✗ → super ✓ — solved by super in 7 attempt(s) — $0.0021 (Ultra-only equivalent $0.0094), 41.2s",
  "error": null
}
```

- `status`: `solved` (code passes every test), `failed` (best draft is in `code`, but it does not pass), or
  `error` (API problem; see `error`).
- `summary` starts with the rung path, one mark per round: under `ladder-wide`, a Nano round is 3 parallel
  drafts, so `attempts` can be larger than the number of marks.
- While it runs, the server sends an MCP progress notification (with a message such as
  `escalating nano → super: ...`) on every model call, sandbox run and escalation, to clients that pass a
  progress token. It does not use MCP logging, which is deprecated as of protocol 2026-07-28.

### `list_policies()`

Each policy from `app.agent.POLICIES` with a one-line description and its rungs: `ladder`, `ladder-wide`
(default), `ladder-auto`, `nano-only`, `super-only`, `ultra-only`.

## Example prompts

> Use ladder to write a function `parse_duration(s: str) -> int` that turns strings like "1h30m", "45s" and
> "2h" into seconds and raises ValueError on anything else. Then add it to utils/time.py.

> Here are my tests in tests/test_roman.py. Use ladder's solve_function with those tests to implement
> `to_roman(n)`, using policy ladder-auto.

The assistant still owns the integration: Ladder returns one standalone, standard-library module verified
against the tests, and the assistant pastes it where it belongs.
