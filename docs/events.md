# Agent event stream (contract)

`Agent.solve(task, policy)` (app/agent.py) is an async generator of JSON-serialisable dicts. The web UI
(`/api/solve`, Server-Sent Events, one `data: <json>` frame per event), the CLI and the benchmark all
consume the same stream. Every event has a `type`.

| type | fields | meaning |
|---|---|---|
| `start` | `task_id, policy{name, ladder[], start, attempts_per_tier, test_tier, width{tier:int}}, models{tier:model_id}` | first event |
| `call` | `role ("triage"\|"tests"\|"code"), tier, model, sid`, and for code: `attempt, candidate, width` | a model call starts; `sid` identifies this call's stream |
| `delta` | `sid, kind ("reasoning"\|"content"\|"reset"), text` | streamed tokens of call `sid`. Consecutive fragments are coalesced. `reset` means the call was retried: discard text received so far for `sid` |
| `llm` | `sid, role, tier, model, tokens_in, tokens_out, cost, latency_s, content, reasoning, finish_reason, usage_estimated`, (+ `attempt, candidate, width` for code) | call `sid` finished; `content`/`reasoning` are the final (clipped) texts |
| `triage` | `difficulty, start, why` | Nano picked the starting rung (policy `ladder-auto`) |
| `tests` | `tests, source ("user"\|"generated"\|"revised by <tier>"), why?` | the active test file |
| `run` | `sid, tier, attempt, candidate, width, code, ok, summary, tests_revised, report{passed, failed, fatal, duration_s, tests[{name, ok, error, where}]}` | sandbox result for draft `sid` |
| `pick` | `tier, attempt, candidate, width, ok, scores[{candidate, passed, total, fatal}]` | only when width > 1: which parallel draft won the round |
| `escalate` | `from, to, reason` | climbing to the next rung |
| `error` | `message`, optional `sid`, `recoverable` | `recoverable: true` = one parallel draft failed, the run continues |
| `done` | `status ("solved"\|"failed"\|"error"), solved_by, code, tests, attempts, rounds, cost, ultra_equivalent_cost, tokens_in, tokens_out, llm_seconds, wall_seconds, by_tier{tier:{calls,cost,tokens}}, calls[]` | last event |

Within a round of width *k*, the *k* drafts run concurrently, so their `call`/`delta`/`llm`/`run` events
interleave. Group them by `sid` (`code-<attempt>`). Attempts are numbered globally across rounds; a round's
drafts are `attempt = first..first+k-1` with `candidate = 0..k-1`.

Policies (`app.agent.POLICIES`): `ladder`, `ladder-wide` (3 parallel Nano drafts per round), `ladder-auto`,
`nano-only`, `super-only`, `ultra-only`.
