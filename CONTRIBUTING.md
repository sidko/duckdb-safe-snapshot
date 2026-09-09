# Contributing

Use Python 3.10–3.12 on Linux. Install test dependencies in an isolated
environment, then run:

```bash
python -m pytest
```

Tests create synthetic DuckDB databases and snapshot directories only. Do not
add real databases, production paths, credentials, or provider data. By
submitting a contribution, you agree that it may be distributed under Apache-2.0.
