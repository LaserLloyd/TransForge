# StudioForge contract tests (TransForge)

`tests/test_contract_studioforge.py` pins the exact StudioForge behaviour
TransForge depends on, using the small JSON fixtures in `fixtures/`.

**Provenance: every fixture is derived from the client code's expectations**
(what `transforge.py` reads and branches on), not recorded from a live server.
That is the point: if a StudioForge change breaks one of these shapes, the
matching test here fails visibly in TransForge's CI, and the person making the
StudioForge change has to decide between "revert" and "update the fixture and
the client together".

| Fixture | What the client depends on |
|---|---|
| `error_gpu_leased.json` | `error.code == "gpu_leased"`, `error.studioforge.retry_after_s`, `error.studioforge.lease{holder,kind,holder_family}`, `Retry-After` header |
| `error_priority_hold.json` | `priority_hold` is retried after `retry_after_s` |
| `error_insufficient_vram.json` | `insufficient_vram` is final (never retried) |
| `error_context_exceeded.json` | `context_exceeded` is final |
| `leases.json` | `GET /api/leases -> {"leases":[...]}`; per-record `kind`, `holder_family` (+ `holder` fallback) |
| `models.json` | `GET /v1/models -> data[].id`; additive fields are ignored |
| `status.json` | `GET /api/status -> loaded[].{model_id,state,plan.parallel,plan.ctx_per_slot}` |
| `load_recommended.json` | `POST /api/models/{quoted id}/load-recommended` body `{ctx_size, priority}` |
| `chat_completion.json` | `POST /v1/chat/completions` request keys (`priority`, `chat_template_kwargs`), `X-SF-Client` |

Keep the fixtures small. Add a field only when the client starts depending on it.
The tests run an in-process fake StudioForge on an ephemeral loopback port
(`tests/_fakesf.py`); the suite's socket guard (`tests/_netguard.py`) forbids
any other connection, so these tests can never reach a real StudioForge.
