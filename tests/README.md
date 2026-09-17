# Tests

Every test in this directory must be written by a team member, by hand.

The brief grades hand-written tests at ten points each and scores a test written
by Claude or Codex at zero. That covers test bodies, assertions, fixtures and
`conftest.py`. AI tools may run the tests and explain failures; they do not
write them.

Run with:

```bash
uv run pytest
```

Until the first test exists, pytest exits with code 5 ("no tests collected").
