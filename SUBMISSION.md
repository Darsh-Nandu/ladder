# Devpost submission kit: Ladder

## Track
Coding and Agentic Engineering

## Tagline (≤ 200 chars)
A coding agent that writes, runs and tests code, starts on Nemotron 3 Nano and climbs to Super and Ultra only when the tests fail. Ultra-class reliability at a fraction of Ultra's cost.

## What it does
Ladder is a coding agent for everyday function-level work: utilities, parsers, algorithms, data transforms. You describe the function (and optionally give your own tests). Ladder then:

1. has Nemotron 3 Super write a unit-test suite from the spec, if you gave none;
2. asks **Nemotron 3 Nano** for a full solution;
3. runs it against the tests in a locked-down sandbox (no network, CPU and memory limits, no secrets);
4. feeds the exact failing assertion back for a repair;
5. after two failures, **climbs** to **Nemotron 3 Super** with reasoning on, and then to **Nemotron 3 Ultra**. Each rung inherits the full failure history.

Every model call streams live to the UI, token by token: which rung, the Nemotron reasoning as it happens, the tokens, the dollars and the latency. Parallel drafts appear side by side, and each sandbox run shows a pass/fail chip per test. The final screen compares what you paid with what the same tokens would have cost on Ultra.

The same engine has **four front-ends**:
- the **web app**, with a replay Gallery of recorded runs;
- a **CLI**;
- **repo mode**: point it at a real project with a failing `pytest` suite and it patches the source (never the tests) in a sandboxed copy until the suite is green;
- an **MCP server**, so Claude Code, Cursor or any MCP client can delegate "write me a verified function" to Ladder.

## Why we built it
The hackathon brief says it plainly: *reach for Ultra when you need serious reasoning, and let Nano or Super handle the fast, everyday calls.* Nearly every agent hard-codes one model, though, so the routing decision is made once by a developer instead of on every request. Ladder makes that decision for every request, using the most honest signal a coding agent has: **whether the tests pass.** No learned router, no guesswork. Compute is spent only where verification shows it is needed.

## How it works / how we built it
- **Nebius Token Factory** (OpenAI-compatible API) serves all three Nemotron 3 models behind one endpoint and one key. Switching model per attempt is a single field change, which is what makes per-request escalation practical.
- **Nemotron reasoning control**: reasoning is off on Nano (fast, cheap) and on for Super and Ultra, via `chat_template_kwargs.enable_thinking` with a `/think` / `/no_think` fallback. Reasoning traces (`reasoning_content`) appear in the UI.
- **Sandbox**: each attempt runs in a fresh temp dir via `unshare -rn` (no network), with rlimits on memory, CPU and file size, a wall-clock kill, and an empty environment. A small harness reports per-test results as JSON.
- **Width before height** (`ladder-wide`): three parallel Nano drafts cost less than one Super call, so the agent tries in width before it climbs.
- **Nano triage** (`ladder-auto`, the default): Nano rates difficulty and picks the starting rung. This is the best policy on the benchmark.
- **Reasoning salvage**: when a reasoning model spends its whole token budget thinking, Ladder hands the analysis back with thinking off and asks for just the code, instead of discarding the attempt.
- **Anti-cheating**: user tests are immutable. Generated tests can be revised only by Super or Ultra, with a visible one-line justification. Benchmark scoring always uses hidden tests the agent never sees.
- **Stack**: Python, FastAPI with Server-Sent Events, httpx, and a single-page vanilla-JS UI. Docker image deployable as a CPU Nebius Serverless Endpoint.
- **Benchmark**: 24 original tasks (8 easy, 8 medium, 8 hard), each with a reference solution and validated hidden tests. Five policies are compared: ladder, ladder + Nano triage, and Nano/Super/Ultra-only.

## Results
24 original tasks (8 easy, 8 medium, 8 hard), scored on hidden tests the agent never sees:

| Policy | Solved | Hard | Total cost | Avg time |
|---|---|---|---|---|
| **Ladder + triage** | **24/24** | **8/8** | $0.27 | 130 s |
| Ultra only | 21/24 | 5/8 | $0.52 | 361 s |
| Super only | 21/24 | 5/8 | $0.16 | 88 s |
| Ladder | 20/24 | 5/8 | $0.19 | 173 s |
| Nano only | 9/24 | 1/8 | $0.016 | 25 s |

- Ladder + triage solved **every task**, at **52% of Ultra-only's cost** and **2.8× faster**. Raw model size (Ultra only) was neither the most accurate nor fast.
- Plain Ladder finished **17 of 20 solves on Nano**.
- Repo mode, live: on the bundled project with 3 seeded bugs, Nano fixed 1, Ladder escalated, and Super fixed the other 2. 13/13 tests passed for $0.0025 in 25 s.
- Reasoning salvage (added after the benchmark run): the hardest regex task went from needing Ultra ($0.069) to being solved on Super ($0.015).

## Challenges
- Generated tests can be wrong, and a wrong test sends the agent up the ladder for nothing. We added spec-only test-writing rules, routed test-writing to Super, and allowed justified test repair only on higher rungs.
- Running untrusted model output on a public demo: network namespace isolation, rlimits, and keeping the API key out of the child process.
- Making cost honest: every number in the UI comes from Token Factory's own `usage` field, multiplied by the published per-model prices.

## What's next
- Parallel drafts in repo mode, plus a GitHub App that runs repo mode on failing CI.
- Learn per-task-type start rungs from benchmark history (the `ladder-auto` policy is the first step).
- Run benchmark sweeps as Nebius Serverless Jobs.

## Built with
nvidia-nemotron · nemotron-3-nano · nemotron-3-super · nemotron-3-ultra · nebius-token-factory · nebius-serverless · python · fastapi · server-sent-events · docker

---

## 3-minute demo video script

The Gallery holds real recorded runs (`runs/featured/`), so every shot can be filmed from replays, without waiting on live calls. Use a live run for at least one shot to show it's real.

| Time | Shot | Voice-over |
|---|---|---|
| 0:00–0:15 | Title, then the UI | "Coding agents send every request to the biggest model. Ladder starts on the smallest Nemotron and climbs only when the tests prove it has to." |
| 0:15–0:40 | **Live**: pick the LRU-cache example, policy *Ladder · wide*, Run | "Super writes the tests; three Nano drafts stream in parallel; the sandbox runs each one. All pass, for a tenth of a cent." Point at Spent vs. Same tokens on Ultra. |
| 0:40–1:25 | Gallery → **mini-regex-ladder** replay | "A hard one. Nano's drafts fail; you can see the exact assertions. Escalate to Super, with its reasoning streaming. Then escalate to Ultra, which passes 14 of 14." |
| 1:25–1:45 | Gallery → **expression-evaluator-ladder** | "Here Super was enough. The ladder paid for exactly what the problem needed." |
| 1:45–2:10 | Benchmark tab | "24 tasks, scored on hidden tests the agent never sees. Ladder + triage: 24 of 24. Ultra alone: 21, at twice the cost and almost three times the time." |
| 2:10–2:35 | Terminal: `python -m app.repo_agent examples/buggy_inventory` | "Repo mode: a real project with three bugs. Nano fixes one, Super the rest, and the suite goes green. Here's the diff." |
| 2:35–2:50 | Terminal: `claude mcp add ladder …`, then a Claude Code prompt | "And it's an MCP server, so your coding assistant can delegate verified functions to Nemotron." |
| 2:50–3:00 | README | "NVIDIA Nemotron 3 Nano, Super and Ultra, served by Nebius Token Factory. Open source, Apache-2.0." |

---

## Feedback on Nebius Token Factory and NVIDIA models (draft; edit with your own experience)
- **Good:** one OpenAI-compatible endpoint for every Nemotron size made per-request routing trivial. The `usage` field made exact cost accounting easy.
- **Good:** Nemotron 3's switchable reasoning is a strong fit for tiered agents: one model family, with reasoning paid for only where it's needed.
- **Wish:** a machine-readable price field in `/v1/models` would let apps like Ladder price calls without hard-coding a price table.
- **Wish:** clearer docs on how reasoning is toggled and returned per model (`chat_template_kwargs` vs. system tags; `reasoning_content` vs. inline `<think>`). We handle every variant defensively.
- **Wish:** a documented per-request `reasoning_budget` across all Nemotron sizes would allow finer-grained escalation (e.g. Super-low → Super-high → Ultra).
