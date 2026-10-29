# Ladder 🪜: an adaptive-compute coding agent on Nebius Token Factory

**Ladder writes, runs and tests code, and climbs from NVIDIA Nemotron 3 Nano → Super → Ultra only when the tests say it has to.**

> Track: **Coding and Agentic Engineering** · Models: **NVIDIA Nemotron 3 Nano / Super / Ultra** · Inference: **Nebius Token Factory** · License: **Apache-2.0**

Most coding agents send every request to the biggest model. That is slow and expensive, and for most everyday functions it isn't needed: a 3B-active Nano model writes a correct `slugify` just as well as a 55B-active Ultra. Ladder treats model size as a **budget that tests unlock**:

1. **Start cheap.** Nemotron 3 Nano (reasoning off, ~$0.06/M input) writes the first solution.
2. **Verify for real.** The code runs against unit tests in a sandbox: no network, CPU and memory limits, a scrubbed environment.
3. **Repair with evidence.** Failures go back to the model with the exact failing assertion and line.
4. **Escalate only on repeated failure.** After two failed attempts the agent climbs to Nemotron 3 Super (reasoning on), and then to Nemotron 3 Ultra. The higher rung inherits the full failure history.
5. **Meter everything.** Every call is metered in tokens, dollars and seconds, and the bill is compared with what the same tokens would have cost on Ultra.

The result is Ultra-class reliability at roughly Nano-class cost for the easy majority of tasks. On the hard tail, it is still cheaper than sending everything to Ultra. The benchmark below measures this on **hidden tests** the agent never sees.

### One engine, four ways to use it

| Surface | What you get |
|---|---|
| **Web app** (`uvicorn app.server:app`) | Live token streaming of every Nemotron call, including the reasoning trace. Parallel drafts appear side by side, with a per-test pass/fail chip for each sandbox run and a live cost meter. A **Gallery** of recorded runs replays through the same view, with `#run=<id>` deep links. |
| **CLI** (`python -m app.cli spec.md`) | The same agent in the terminal, writing the solution to a file. |
| **Repo mode** (`python -m app.repo_agent path/to/project`) | Point it at a real project whose `pytest` suite fails. It patches source files (never tests) in a sandboxed copy and climbs the ladder until the suite is green, then prints the diff (`--apply` writes it back). [docs/repo-mode.md](docs/repo-mode.md) |
| **MCP server** (`python -m app.mcp_server`) | Claude Code, Cursor or any MCP client can delegate "write a verified function" to Ladder and get back code, tests, the rung path and the bill. [docs/mcp.md](docs/mcp.md) |


---

## How it works

```
             ┌──────────── spec (+ optional tests / signature) ────────────┐
             ▼                                                             │
   [Nano] triage difficulty (optional "ladder-auto" policy)               │
             ▼                                                             │
   [Super] write unit tests from the spec  (skipped if you supply tests)  │
             ▼                                                             │
   ┌──► [rung] write full solution ──► sandbox: run tests ──► pass? ──► DONE
   │                                           │ fail
   │      2 failures on this rung? ── no ──────┤ (feed back assertion + line)
   │                 │ yes                     │
   └── climb: Nano ─► Super ─► Ultra ◄─────────┘
```

| Rung | Model (Token Factory id) | Reasoning | Price in / out per 1M | Used for |
|---|---|---|---|---|
| Nano | `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B` | off | $0.06 / $0.24 | triage, first attempts |
| Super | `nvidia/nemotron-3-super-120b-a12b` | on | $0.30 / $0.90 | test writing, second rung |
| Ultra | `nvidia/Nemotron-3-Ultra-550b-a55b` | on | $1.00 / $3.00 | hardest failures only |

**Guardrails against "cheating":**
- Tests you supply are immutable. Generated tests can be revised only by Super or Ultra, only if they provably contradict the spec, and every revision appears in the trace with a one-line justification.
- Benchmark scoring always uses separate **hidden tests**, so revising visible tests cannot inflate the score.
- Model-written code never sees the API key: the sandbox child process gets an empty environment.

**Policies** (pick one in the UI or with `--policy`):

| Policy | Behaviour |
|---|---|
| `ladder-auto` (default) | Nano triages difficulty first (easy → Nano, medium → Super, hard → Ultra), then climbs on failure. **Best benchmark result: 24/24.** |
| `ladder-wide` | **Width before height**: each round on Nano runs 3 drafts in parallel at different temperatures, the sandbox picks the best, and only then does the agent climb. Three Nano drafts cost less than one Super call. |
| `ladder` | Nano → Super → Ultra, 2 attempts per rung |
| `nano-only` / `super-only` / `ultra-only` | single-model baselines, 4 attempts |

## Benchmark

`bench/tasks.jsonl` holds 24 original tasks (8 easy, 8 medium, 8 hard), e.g. an operator-precedence expression evaluator, an RFC 6901 JSON-pointer resolver, and Dijkstra with lexicographic tie-breaking. Each task has a reference solution and **hidden tests**. `bench/validate_tasks.py` proves every reference passes its hidden tests, and that a stub solution fails at least 70% of them.

```bash
python -m bench.run_bench                 # 6 policies x 24 tasks, writes bench/results/latest.json
python -m bench.run_bench -p ladder ultra-only --difficulty hard
```

The **Benchmark** tab in the UI shows pass rate, cost, $/solve, the rung each solve finished on, and a cost vs. pass-rate scatter plot.

<!-- BENCH_RESULTS_START -->
_24 tasks, run 2026-10-29 15:01 UTC_

| Policy | Solved (hidden tests) | easy | medium | hard | Total cost | $ / solve | Avg time | Solves finished on |
|---|---|---|---|---|---|---|---|---|
| **ladder** | 20/24 (83%) | 8/8 | 7/8 | 5/8 | $0.1909 | $0.0095 | 173.0s | nano 17, super 1 |
| **ladder-wide** | 19/24 (79%) | 8/8 | 5/8 | 6/8 | $0.2708 | $0.0143 | 168.9s | nano 15, super 2, ultra 1 |
| **ladder-auto** | 24/24 (100%) | 8/8 | 8/8 | 8/8 | $0.2746 | $0.0114 | 130.0s | nano 8, super 13, ultra 1 |
| **nano-only** | 9/24 (38%) | 4/8 | 4/8 | 1/8 | $0.0156 | $0.0017 | 25.1s | nano 7 |
| **super-only** | 21/24 (88%) | 8/8 | 8/8 | 5/8 | $0.1635 | $0.0078 | 88.0s | super 21 |
| **ultra-only** | 21/24 (88%) | 8/8 | 8/8 | 5/8 | $0.5236 | $0.0249 | 361.1s | ultra 21 |
<!-- BENCH_RESULTS_END -->

**Reading the results**
- **Ladder + triage solved all 24 tasks (8/8 hard).** Ultra alone solved 21, at **52% of Ultra-only's cost** and **2.8× faster** (130 s vs 361 s per task). Ultra-only is the slowest policy and *not* the most accurate. A repair loop with escalation beats raw model size.
- **Plain `ladder` finished 17 of its 20 solves on Nano**, at 36% of Ultra-only's cost. Most everyday functions never need a big model.
- **Nano alone is not enough (9/24).** Part of the reason: without reasoning, Nano often fails to produce a usable test file. That is why the ladder always writes tests on a stronger rung.
- **Super-only is a strong, cheap baseline (21/24).** The ladder's edge over it is the hard tail, where escalating to Ultra and repairing with evidence closes the last gaps.
- **Salvaging cut-off reasoning (added after this run):** on the hardest tasks, Super sometimes spent its whole token budget thinking and emitted no code, which forced an escalation. Ladder now feeds that reasoning back with thinking off and asks only for the code. In a targeted re-run, `ladder` solved `mini_regex` on Super for $0.015 (it previously needed Ultra, $0.069), and solved `text_justify`, which it had failed before. The table above predates this fix, so it is a conservative baseline.

_Method: hidden tests are never shown to the agent. Run on NVIDIA's free API endpoint, which serves the same Nemotron 3 Ultra and the same-size Nemotron 3.5 Lightning-30B-A3B / 3.5 Super-120B-A12B siblings for the Nano and Super rungs. Costs are computed from the returned token usage at Nebius Token Factory list prices. Latency reflects a shared free endpoint, not Token Factory's dedicated capacity._

## Run it

```bash
git clone <this repo> && cd ladder
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # put your Nebius Token Factory key in NEBIUS_API_KEY
uvicorn app.server:app --port 8000
# open http://localhost:8000
```

**CLI:**

```bash
python -m app.cli spec.md                       # Nemotron writes the tests
python -m app.cli spec.md --tests my_tests.py -o solution.py --policy ladder-auto
```

**Repo mode** (fix a failing test suite; try the bundled example with 3 seeded bugs):

```bash
python -m app.repo_agent examples/buggy_inventory            # dry run: prints the diff
python -m app.repo_agent path/to/your/project --apply        # writes the fix back when the suite passes
```

A live run on `examples/buggy_inventory`: Nano fixed 1 of 3 bugs, Ladder escalated, and Super fixed the remaining two (float rounding → `Decimal` half-up, an off-by-one discount threshold, a sort order). 13/13 tests passed for $0.0025 in 25 s.

**MCP server** (`pip install -r requirements-mcp.txt`):

```bash
claude mcp add ladder -e NEBIUS_API_KEY=$NEBIUS_API_KEY -- "$PWD/.venv/bin/python" -m app.mcp_server
```

**Docker:**

```bash
docker build -t ladder .
docker run -p 8000:8000 -e NEBIUS_API_KEY=... ladder
```

If the server has no key, visitors can paste their own Token Factory key in the UI. It stays in their browser's localStorage and is sent only as a request header to this server, which forwards it to Token Factory.

**Tests** (offline: a mocked Token Factory and a scripted fake LLM drive the real agent loop and sandbox):

```bash
pip install -r requirements-dev.txt && pytest -q
```

### Deploy on Nebius

The app is a CPU-only container, so it fits a **Nebius Serverless Endpoint** on a CPU platform. All GPU work happens in Token Factory. See [`deploy/nebius.sh`](deploy/nebius.sh). In short: push the image to Nebius Container Registry, then `nebius ai endpoint create --image … --container-port 8000 --env NEBIUS_API_KEY=… --public`. Any container host works as well (Render, Fly, Cloud Run).

## Configuration

| Env var | Default |
|---|---|
| `NEBIUS_API_KEY` | (required) |
| `NEBIUS_BASE_URL` | `https://api.tokenfactory.nebius.com/v1`. Pointing it at `https://integrate.api.nvidia.com/v1` (with an `nvapi-` key) switches to NVIDIA's API as a development fallback. There, Nano 3 and Super 3 aren't callable, so their same-size siblings Nemotron 3.5 Lightning 30B-A3B and Nemotron 3.5 Super 120B-A12B are used. |
| `LADDER_NANO_MODEL` / `LADDER_SUPER_MODEL` / `LADDER_ULTRA_MODEL` | ids above. If an id is rejected, Ladder looks up the closest Nemotron id in `/v1/models` |
| `LADDER_SANDBOX_TIMEOUT` / `LADDER_SANDBOX_MEM_MB` | `10` s / `512` MB |
| `LADDER_MAX_CONCURRENT` | `4` concurrent solves per server |

## Project layout

```
app/llm.py       Token Factory client: reasoning toggle, <think> parsing, retries, model-id resolution, cost metering
app/agent.py     the ladder loop: triage → tests → write/run/repair → escalate; emits an event stream
app/sandbox.py   isolated test runner (unshare -rn, rlimits, scrubbed env, per-test results)
app/server.py    FastAPI + Server-Sent Events
app/cli.py       terminal front-end
app/repo_agent.py  repo mode: fix a failing pytest suite in a real project
app/mcp_server.py  MCP server (tools: solve_function, list_policies)
app/runstore.py  recorded runs for replays (runs/, featured replays in runs/featured/)
docs/events.md   the agent event-stream contract shared by UI, CLI, MCP and benchmark
examples/        sample buggy project for repo mode
static/          single-page UI (no build step)
bench/           task set, validator, benchmark runner
tests/           offline tests
```

## How NVIDIA and Nebius are used

- **NVIDIA Nemotron 3 Nano, Super and Ultra** are the three rungs. Ladder uses Nemotron's switchable reasoning (`chat_template_kwargs.enable_thinking`, with a `/think` and `/no_think` system-prompt fallback). Reasoning stays off on Nano for speed and is on for Super and Ultra, and the UI shows the reasoning trace (`reasoning_content`) of each call.
- **Nebius Token Factory** serves every inference call through its OpenAI-compatible API. Having all three sizes behind one endpoint, one key and one request format is what makes per-request model switching a one-line change. Ladder takes advantage of that on every attempt.
- **Nebius Serverless Endpoints** (CPU) host the web app; see `deploy/`.

## License

Apache-2.0, see [LICENSE](LICENSE).
