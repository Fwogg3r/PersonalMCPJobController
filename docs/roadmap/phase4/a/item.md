---
title: Multi-instance connectors (gmail:personal, gmail:work)
status: done
links: []
---
Config key `<type>:<instance>` (e.g. `google:work`) in `connectors:`; the bare key stays the default instance, unchanged.

**Progress**
- Instance id is the whole key: credentials `<type>:<instance>.<field>` (env `PMJC_<TYPE>_<INSTANCE>_<FIELD>`), options, state and event sources are namespaced by it. Existing keys are untouched.
- Tools `<tool>__<instance>` (sanitized, base shortened to 64 chars), description prefixed `[instance] `. Channels, health, `pmjc_connectors` (type/instance fields), `/api/connectors` list instances.
- `pmjc auth set <type>:<instance>`, `PUT /api/connectors/<type>:<instance>/credentials` and the dashboard (Add an instance) create the instance; clearing a non-default instance also removes its settings entry.
- Tests in tests/test_instances.py.
