# Repo mode

`python -m app.repo_agent PATH` points Ladder at a real Python project whose test suite fails. It patches
source files and climbs the Nemotron ladder (Nano -> Super -> Ultra) until the whole suite passes.

```bash
python -m app.repo_agent examples/buggy_inventory                 # dry run on a temp copy, prints the diff
python -m app.repo_agent examples/buggy_inventory --apply         # write the fix back if the suite passes
```

| option | default | meaning |
|---|---|---|
| `--policy` | `ladder` | any of `app.agent.POLICIES`; `attempts_per_tier` rounds per rung, then climb |
| `--test-cmd` | `python -m pytest -q -p no:cacheprovider --tb=short` | run from the project root; a leading `python` is replaced by Ladder's own interpreter |
| `--apply` | off | copy the changed files back into PATH, only when the suite passes |
| `--allow-test-edits` | off | let the model edit test files (`tests/`, `test/`, `test_*.py`, `*_test.py`, `conftest.py`) |
| `--max-context` | 40000 | characters of repo text shown to the model per attempt |
| `--timeout` | 60 | seconds per test-suite run |

## How it works

1. The repo is copied to a temp dir (`.git`, `.venv`, `venv`, `node_modules`, `__pycache__`, `*.pyc`,
   `.pytest_cache` skipped; symlinks copied as links). A baseline test run follows; a green suite ends immediately.
2. Each attempt the model gets the test command, a ~4000-char failure summary (counts, `FAILED`/`ERROR` ids,
   then as many per-test tracebacks as fit, collection errors first), the repo file tree (`.py/.toml/.cfg/.md`),
   and full file texts in priority order: source files named in tracebacks, files already changed, the failing
   test files, then repo files they import (following imports three levels deep, so `from pkg import x` pulls in
   `pkg/__init__.py` and the modules it re-exports). Files that do not fit in `--max-context` are listed as omitted.
3. The model answers with a one-or-two-sentence diagnosis and one ` ```python ` block per changed file whose first
   line is `# FILE: relative/path.py` (full-file replacement).
4. Edits are applied to the work copy and the suite re-runs. An attempt is **kept** if it scores at least as well
   as the best state so far (green > no fatal > more passes > fewer failures); otherwise it is reverted. The
   next prompt shows the attempt's diff, whether it was kept or reverted, and any rejected edits.
5. After `attempts_per_tier` rounds on a rung the agent escalates. On success the `done` event carries the unified
   diff against the original; with `--apply` the changed files are written back.

## Events

`RepoAgent.solve(path, policy)` emits the [event protocol](events.md) (`start`, `call`, `delta`, `llm`, `run`,
`escalate`, `error`, `done`) with these differences:

- `start` adds `repo` and `test_cmd`; `policy.width` is `{}` (repo mode always uses one draft per round).
- `run` for the initial suite run has `sid: "baseline"`, `tier: null`, `attempt: 0`. Each `run` has `code` = that
  attempt's diff, `changed_files`, `rejected[]`, `kept`, and `report{passed, failed (failed + errors), fatal,
  duration_s, exit_code, tests[]}` (only failing tests are listed).
- `done` has `diff`, `changed_files`, `applied`, `test_cmd` and the final `report` instead of `code`/`tests`.

## Safety model

- **The original is read-only** unless `--apply` is given *and* the suite passes; even then only changed files
  are written, after their paths are validated again against the original.
- **Path validation** for every edit: relative, normalised, no `..` escape, no absolute or `~` paths, `.py` only,
  no hidden/ignored directories, and the resolved path (symlinks followed) must stay inside the work copy.
  At most 8 files per attempt. Test files are read-only by default. Rejections are reported back to the model.
- **No faking the runner**: a new file may not shadow a module the test command imports (the repo root is first on
  `sys.path`): `pytest`, `_pytest`, `pluggy`, `py`, `iniconfig`, `packaging`, `pygments`, `conftest`, any stdlib
  module, the module after `-m`, and `sitecustomize`/`usercustomize` anywhere. Existing repo files keep their names.
- **Answer parsing** pairs fences line by line: ` ```text ` / ` ```bash ` blocks are skipped without shifting later
  blocks, a block closes only on a bare fence at its own indentation (an indented fence in a docstring is content),
  and an unclosed `# FILE:` block (answer cut off) is rejected rather than written truncated.
- **Work-copy I/O** never reads or writes through a symlink that leaves the work copy (the suite may run as another
  uid and plant one); FIFOs, sockets and devices in the repo are not copied. Any unexpected exception still ends the
  stream with `error` then `done`.
- **Test runs** use `app.sandbox.run_command`: no network (`unshare -rn` when available), CPU / memory /
  file-size rlimits, own process group killed on timeout and after the command exits (leftover children
  neither linger nor turn a finished run into a timeout), scrubbed environment (`PATH`, `HOME` = work dir,
  `PYTHONDONTWRITEBYTECODE=1`; no API keys) and, when running as root, the `LADDER_SANDBOX_UID` uid drop.
  Output is kept to the last 20 kB per stream.

## Limitations

- The sandbox is not a chroot: test code can read any file the user can read, and without `unshare` the
  network is open (the Python-level socket block of `run_tests` does not apply to arbitrary suites). Only run
  repo mode on code you would run yourself.
- The project's dependencies must be importable by Ladder's interpreter (install them into Ladder's venv), and
  the memory limit (`LADDER_SANDBOX_MEM_MB`, default 512) applies to the whole suite.
- Full-file replacements: large files cost output tokens (Nano's `max_tokens` is 4096) and big files may not fit
  the context budget. No file deletion or renames.
- `width > 1` (`ladder-wide`) is treated as one draft per round, and `ladder-auto` starts on the first rung
  without triage.
- pytest output is parsed for counts and failing ids; other test commands still work (success = exit code 0)
  but the model only sees the raw output tail.
