# buggy_inventory

A small warehouse-inventory package with **three seeded bugs** in two files, used to demo Ladder's repo mode.
Its pytest suite has 13 tests; 5 fail out of the box.

| file | bug |
|---|---|
| `inventory/pricing.py` | `to_cents` rounds through `float` (`2.675 -> 2.67`, `0.125 -> 0.12`, `5 -> "5.0"`) instead of `Decimal` half-up to two places |
| `inventory/pricing.py` | the bulk discount starts one unit too late (`>` instead of `>=` the threshold) |
| `inventory/report.py` | the stock report sorts ties by SKU descending (`reverse=True` on the whole key) |

## Run

From the Ladder repo root:

```bash
# see the failures
(cd examples/buggy_inventory && python -m pytest -q)

# let Ladder fix them on a temp copy (the example is never modified)
python -m app.repo_agent examples/buggy_inventory

# write the fix back into the example (only if the whole suite passes)
python -m app.repo_agent examples/buggy_inventory --apply
```

Tests are off limits for the agent unless you pass `--allow-test-edits`. If you used `--apply`, restore the
bugs with `git checkout examples/buggy_inventory`.
