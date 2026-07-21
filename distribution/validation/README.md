# HERMES-TEAM validation contract

The public-core gate discovers every `test_*.py` under `tests/` except the
upstream runner's separately managed `integration`, `e2e`, and `docker`
suites. It then applies the single fail-closed exclusion manifest:

```bash
scripts/run_tests.sh -j 8 \
  --exclude-manifest distribution/validation/test_exclusions.json
```

Every exclusion needs a reason. File globs must match at least one discovered
file and exact node IDs must still exist in Python syntax; otherwise the run
fails before tests start. New upstream tests are included automatically.

Current exclusions are limited to:

- test files that import the optional `agent-client-protocol`/ACP extra, which
  is not part of the HERMES-TEAM public-core install;
- three exact upstream Anthropic OAuth setup tests whose global
  `subprocess.run` mock also intercepts the macOS keychain reader.

Context compression, Timeline, Code Map, NeuralLink, Heartbeat, Role Shell,
Binding, Receipt, and adapter tests are not excluded. Separate upstream
integration/e2e/docker jobs remain separate by the inherited test-runner
contract; they are not represented as HERMES-TEAM exceptions.
