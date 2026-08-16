# SaaS Integration Reliability & Support Troubleshooting

> **Synthetic local work sample. All systems, records, failures, and outcomes are fictional; no real customer or production integration is represented.**

This repository demonstrates the analysis and support judgment behind a small account-to-support entitlement integration. A CRM-like source owns fictional account state; a support-entitlement target receives a deterministic transformation with source version, event identity, and correlation lineage retained for investigation.

The emphasis is not code volume. It is the ability to make integration behavior explainable: define contracts, resolve mapping precedence, prevent duplicate or stale writes, bound retries, preserve dead-letter and replay history, reconcile complete state, and hand useful evidence to support.

## Execution authority

The Python reference orchestrator is the only executed workflow in this repository. It coordinates three local FastAPI mocks through HTTPX ASGI transports and records state in temporary SQLite databases.

**n8n runtime execution was deferred.** The included [`n8n_entitlement_sync_workflow.draft.json`](04_Integration_Workflow/n8n_entitlement_sync_workflow.draft.json) is an unexecuted structural draft. It is included to show a portable workflow design, not as execution evidence or a claim of n8n proficiency.

## What the proof covers

- strict source and target OpenAPI contracts;
- explicit entitlement, priority, region, and product mapping rules;
- API-key rejection before target delivery;
- idempotency and out-of-order protection;
- bounded retry for modeled `429` and `503` responses;
- permanent rejection for authentication and contract failures;
- dead-letter review and replay with linked correlation;
- controlled test-only repair from the latest valid source state;
- full-universe reconciliation; and
- an investigation-ready runbook with three synthetic incidents.

## Architecture and evidence

- [Architecture and reliability model](docs/ARCHITECTURE.md)
- [Mapping and reliability rules](05_Data_Mapping_and_Contracts/PORT0003_Mapping_and_Reliability_Rules.csv)
- [Twelve-scenario fixture](06_Test_Fixtures/PORT0003_12_Scenario_Test_Matrix.json)
- [Execution and reconciliation evidence](evidence/execution-and-reconciliation.json)
- [Structured execution trace](evidence/structured-execution-trace.jsonl)
- [Support runbook and incident walkthroughs](docs/SUPPORT_RUNBOOK.md)
- [Case study](CASE_STUDY.md)
- [Limitations](LIMITATIONS.md)

The committed deterministic run records 12 passing scenarios. Its target-attempt vector is `[1, 1, 1, 0, 0, 0, 0, 2, 3, 0, 0, 1]`, and final reconciliation contains three matches with zero exceptions.

## Reproduce locally

Use CPython 3.11. The dependency lock contains exact versions from the accepted local run.

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install --requirement dependency-lock.txt
$env:PORT0003_LOCAL_API_KEY = 'replace-with-local-test-value'
.\.venv\Scripts\python.exe -B -m pytest -q -p no:cacheprovider
.\.venv\Scripts\python.exe -B validation\validate_repository.py
```

Unix-like shells can set the same local placeholder with `export PORT0003_LOCAL_API_KEY=replace-with-local-test-value` and invoke the environment's `python` executable.

To regenerate evidence without overwriting the committed proof:

```powershell
.\.venv\Scripts\python.exe -B 04_Integration_Workflow\reference_orchestrator.py `
  --fixtures 06_Test_Fixtures\PORT0003_12_Scenario_Test_Matrix.json `
  --runtime-dir runtime `
  --evidence-out runtime\execution-and-reconciliation.json `
  --trace-out runtime\structured-execution-trace.jsonl
```

The `.env.example` file documents only the executed Python path. The n8n draft references separate inbound, state, target, and URL environment variables for structural portability; those references are not a runnable n8n configuration and no credential is embedded.

## Expected validation result

The repository-specific validator checks topology, contracts, mapping rules, service authentication, the exact scenario set, deterministic execution, retries, dead-letter/replay lineage, reconciliation, public documentation, links, and privacy/secret/path boundaries. A clean run prints:

`PASS_PORT0003_PUBLIC_TECHNICAL_REPOSITORY`
