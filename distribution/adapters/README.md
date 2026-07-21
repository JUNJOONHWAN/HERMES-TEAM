# Adapter templates

- `codex-cli.json` registers Codex as an optional worker. It never stores a
  token; the operator's existing Codex authentication is used.
- Grok is pre-registered as the disabled `controller_grok` controller candidate
  and is enabled only after `XAI_API_KEY` and a live catalog/tool probe succeed.
- OpenRouter is pre-registered as the disabled
  `controller_openrouter_gemma4` candidate. It is not the default.
- A local OpenAI-compatible endpoint is pre-registered as the disabled
  `controller_vllm_gemma4` candidate.
- `generic-command.example.json` is the contract for any CLI that consumes a
  prompt file or prompt text and returns its final answer on stdout.

Register a reviewed command adapter:

```bash
python3 scripts/register_external_adapter.py distribution/adapters/codex-cli.json
```

Registration never makes a new adapter permanent or primary. It creates
candidate bindings, runs its declared health probe, and enables it only when the
probe passes. Use the Hermes once/temporary/permanent override controls after
reviewing the route.
