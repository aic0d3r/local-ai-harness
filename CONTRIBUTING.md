# Contributing

Bug reports with the diagnostics asked for in the issue form are the most useful contribution. Setup questions go in
[Discussions](https://github.com/aic0d3r/local-ai-harness/discussions).

For a pull request:

- Keep it small and say what you measured. Claims in the README and docs link to a logged run in
  [docs/BENCHMARKS.md](docs/BENCHMARKS.md); new claims should too.
- Add a line to `CHANGELOG.md` under the top version section, in its existing Added/Changed/Fixed block.
- Run the tests before you push:

  ```bash
  for t in tests/test_*.py; do python3 "$t" || break; done   # each prints "ok"; plain scripts, run them like this, not with pytest
  bun test tests/                                            # extension tests
  bash tests/e2e-setup.sh                                    # setup.sh against a stub engine in a throwaway HOME (needs :8080 free)
  bash tests/upgrade-osmos.test.sh                           # scripts/upgrade-osmos.sh against a fake libosmos repo (needs :18091 free)
  ```

  CI (`.github/workflows/ci.yml`) runs the same four plus `tests/upgrade-strata.test.sh`, `tests/upgrade-llamacpp.test.sh` and `shellcheck -S error` on every push to master and every pull request.

- New hardware or engine paths: say what you tested on and what you didn't, as the Windows page does.
