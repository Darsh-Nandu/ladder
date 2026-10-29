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

Every model call streams live to the UI: which rung, the tokens, the dollars, the latency and the Nemotron reasoning trace. Each sandbox run shows a pass/fail chip per test. The final screen compares what you paid with what the same tokens would have cost on Ultra.

## Why we built it
The hackathon brief says it plainly: *reach for Ultra when you need serious reasoning, and let Nano or Super handle the fast, everyday calls.* Nearly every agent hard-codes one model, though, so the routing decision is made once by a developer instead of on every request. Ladder makes that decision for every request, using the most honest signal a coding agent has: **whether the tests pass.** No learned router, no guesswork. Compute is spent only where verification shows it is needed.

## How it works / how we built it
- **Nebius Token Factory** (OpenAI-compatible API) serves all three Nemotron 3 models behind one endpoint and one key. Switching model per attempt is a single field change, which is what makes per-request escalation practical.
- **Nemotron reasoning control**: reasoning is off on Nano (fast, cheap) and on for Super and Ultra, via `chat_template_kwargs.enable_thinking` with a `/think` / `/no_think` fallback. Reasoning traces (`reasoning_content`) appear in the UI.
- **Sandbox**: each attempt runs in a fresh temp dir via `unshare -rn` (no network), with rlimits on memory, CPU and file size, a wall-clock kill, and an empty environment. A small harness reports per-test results as JSON.
- **Anti-cheating**: user tests are immutable. Generated tests can be revised only by Super or Ultra, with a visible one-line justification. Benchmark scoring always uses hidden tests the agent never sees.
- **Stack**: Python, FastAPI with Server-Sent Events, httpx, and a single-page vanilla-JS UI. Docker image deployable as a CPU Nebius Serverless Endpoint.
- **Benchmark**: 24 original tasks (8 easy, 8 medium, 8 hard), each with a reference solution and validated hidden tests. Five policies are compared: ladder, ladder + Nano triage, and Nano/Super/Ultra-only.

## Results
_Paste the table from `python -m bench.run_bench` (also visible in the Benchmark tab)._

## Challenges
- Generated tests can be wrong, and a wrong test sends the agent up the ladder for nothing. We added spec-only test-writing rules, routed test-writing to Super, and allowed justified test repair only on higher rungs.
- Running untrusted model output on a public demo: network namespace isolation, rlimits, and keeping the API key out of the child process.
- Making cost honest: every number in the UI comes from Token Factory's own `usage` field, multiplied by the published per-model prices.

## What's next
- Repository mode: apply the same ladder to a failing test in a real repo (patch → `pytest` → escalate).
- Learn per-task-type start rungs from benchmark history (the `ladder-auto` policy is the first step).
- Run benchmark sweeps as Nebius Serverless Jobs.

## Built with
nvidia-nemotron · nemotron-3-nano · nemotron-3-super · nemotron-3-ultra · nebius-token-factory · nebius-serverless · python · fastapi · server-sent-events · docker

---

## 3-minute demo video script

| Time | Shot | Voice-over |
|---|---|---|
| 0:00–0:15 | Title, then the UI | "Coding agents send every request to the biggest model. Ladder runs on Nebius Token Factory and starts on the smallest Nemotron, earning its way up only when tests fail." |
| 0:15–0:45 | Pick the **easy** example, Policy *Ladder*, Run | "Super writes tests from the spec, Nano writes the code, and the sandbox runs it. All green on the first try for a fraction of a cent." Point at Spent vs. Same tokens on Ultra. |
| 0:45–1:40 | Pick a **hard** example (expression evaluator), Run | Narrate: Nano fails, the red chips show the exact failing assertion, Nano retries, then the **escalate** banner. Super's reasoning trace expands, and it passes. "The agent paid for Super only because the tests proved Nano couldn't do it." |
| 1:40–2:05 | Same hard task, Policy *Ultra only* | "The same task on Ultra alone: same answer, several times the bill." |
| 2:05–2:35 | **Benchmark** tab | "Across 24 tasks scored on hidden tests the agent never sees: Ladder's pass rate versus Ultra-only, at X% of the cost. Most solves finish on Nano." |
| 2:35–2:50 | Terminal: `python -m app.cli spec.md -o solution.py` | "Also a CLI, so it fits a developer workflow." |
| 2:50–3:00 | README / architecture | "Nemotron 3 Nano, Super and Ultra on Nebius Token Factory, deployed on Nebius Serverless. Open source, Apache-2.0." |

---

## Feedback on Nebius Token Factory and NVIDIA models (draft; edit with your own experience)
- **Good:** one OpenAI-compatible endpoint for every Nemotron size made per-request routing trivial. The `usage` field made exact cost accounting easy.
- **Good:** Nemotron 3's switchable reasoning is a strong fit for tiered agents: one model family, with reasoning paid for only where it's needed.
- **Wish:** a machine-readable price field in `/v1/models` would let apps like Ladder price calls without hard-coding a price table.
- **Wish:** clearer docs on how reasoning is toggled and returned per model (`chat_template_kwargs` vs. system tags; `reasoning_content` vs. inline `<think>`). We handle every variant defensively.
- **Wish:** a documented per-request `reasoning_budget` across all Nemotron sizes would allow finer-grained escalation (e.g. Super-low → Super-high → Ultra).
