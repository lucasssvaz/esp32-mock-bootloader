# Test coverage reports

Coverage measures how well the **test suite exercises the mock bootloader
implementation** (`server`, `cli`, `daemon`, `registers`, …). It does **not**
include `esp32_mock_bootloader.testing` — that subpackage is integration helper
code used *by* the tests (and by downstream CI consumers), not the ROM mock
itself. Helpers are omitted via `omit` in `pyproject.toml`.

## Local run

```bash
pip install -e ".[test]"
pytest --cov=esp32_mock_bootloader \
  --cov-report=term-missing \
  --cov-report=html:reports/htmlcov \
  --cov-report=xml:reports/coverage.xml
python scripts/check_coverage.py
python scripts/coverage_gaps.py
```

`coverage_gaps.py` prints uncovered **lines and function names** for product
code (same scope as the baseline — `testing/` omitted). Use
`reports/htmlcov/index.html` for line-by-line highlighting.

Tests run in parallel by default (`-n auto` in `pyproject.toml`; requires `pytest-xdist` from `.[test]`).
Subprocess coverage (CLI/daemon tests) requires `patch = ["subprocess"]` in `pyproject.toml` (pytest-cov 7+).

Outputs:

| Path | Purpose |
|------|---------|
| `reports/.coverage*` | Raw coverage data (gitignored) |
| `reports/coverage.xml` | Machine-readable (gitignored) |
| `reports/htmlcov/` | HTML report (gitignored) |
| `reports/coverage-summary.json` | Latest run vs baseline (gitignored) |
| `reports/coverage-gaps.json` | Uncovered lines/functions per module (gitignored) |
| `reports/coverage-baseline.json` | **Committed** minimum line % thresholds |

## CI

The `test` matrix job runs the full suite with coverage on every OS. Ubuntu checks
against `coverage-baseline.json` and uploads `reports/htmlcov` as an artifact.

## Updating the baseline

After adding tests and confirming coverage improved:

1. Run the local commands above.
2. Edit `coverage-baseline.json` with new minimums (do not lower without reason).
3. Commit the updated baseline.

Baseline shape:

- **`total`** — project-wide floor for product code.
- **Per-module floors** — only for large or high-risk modules where a drop would
  not move `total` much (`server`, `registers`, `com0com`, …). Tiny constant
  modules and `testing/*` are intentionally excluded.
