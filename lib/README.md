# lib/

This directory holds internal shared libraries and utilities that are not part
of the `src` application layer but are reused across multiple modules.

**Planned contents (to be populated as the project grows):**

- `lib/schemas/` — Python wrappers around the JSON Schema validators
- `lib/serializers/` — Avro/Protobuf serializers for the v2 schema migration
- `lib/retry/` — Generic async retry decorators (extracted from producers)
- `lib/testing/` — Shared test fixtures and factory helpers

> This directory is intentionally empty in v0.1.0.
> It is tracked in version control to preserve the intended project structure.
