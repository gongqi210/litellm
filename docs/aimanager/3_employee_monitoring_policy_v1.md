# AiManager Employee Monitoring Policy v1

日期：2026-06-30

## Scope

This document defines the AC-26 governance artifact for employee monitoring around AiManager usage. It covers metadata-only anomaly controls for:

- off-hours usage anomalies;
- suspected key sharing;
- reviewer permissions;
- retention and employee appeal boundaries;
- employee notice acknowledgment evidence.

It does not authorize covert monitoring, prompt-content inspection, response-content inspection, or direct-manager self-service review. Production activation still requires HR/legal approval, employee notice publication, and real acknowledgment records.

## Machine Contract

`aimanager.employee_monitoring.validate_employee_monitoring_controls` validates a policy register, employee roster, and acknowledgment export. `aimanager.scripts.validate_employee_monitoring_policy` exposes the same contract as a CLI.

Required policy sections:

- `policy_id`, `version`, `title`, `published_at`, `effective_at`, `owner`, `notice_url`;
- `notice_channels`, including a real publication path such as WeCom, handbook, intranet, or training;
- `monitored_metadata_fields`, limited to metadata such as employee id, key alias, timestamp, scenario, spend, hashed IP, and hashed user-agent;
- `prohibited_monitoring_fields`, explicitly listing `prompt_text`, `response_text`, `raw_ip`, `raw_user_agent`, and `customer_content`;
- `retention_days`, bounded to 7-180 days;
- enabled `off_hours_usage` and `key_sharing` rules;
- `permission_boundary`, including allowed review roles, prohibited direct-manager access, HR/legal disciplinary guardrail, and employee appeal channel.

Required acknowledgment evidence:

- active employee roster with `employee_id`, `department_id`, and `status`;
- acknowledgment export with `employee_id`, `notice_version`, `acknowledged_at`, `channel`, and all confirmation flags:
  - `understood_purpose`;
  - `understood_scope`;
  - `understood_appeal`;
  - `understood_no_raw_content`.

## Status Semantics

- `PASS`: policy structure is valid, both mandatory rules exist, privacy boundaries are explicit, and every active employee has acknowledged the latest policy version after publication.
- `FAIL`: files exist but contain invalid or contradictory governance, such as prompt/response monitoring, missing mandatory rules, direct-manager review access, missing HR/legal disciplinary boundary, stale acknowledgment version, or missing active employee acknowledgment.
- `BLOCKED`: required evidence files are missing or there is no active employee roster to verify. This must not be treated as approval to run monitoring.

## Production Boundary

Before AC-26 can be treated as production-ready, the company must provide real evidence files and run:

```bash
PYTHONPATH="$PWD" uv run --no-project python -m aimanager.scripts.validate_employee_monitoring_policy \
  --policy-file docs/aimanager/aimanager-employee-monitoring-policy.json \
  --employee-roster-file /path/to/employee-roster.csv \
  --acknowledgment-file /path/to/employee-monitoring-acknowledgments.csv \
  --output-json-file /tmp/aimanager-employee-monitoring.json \
  --output-markdown-file /tmp/aimanager-employee-monitoring.md
```

The committed `docs/aimanager/aimanager-employee-monitoring-policy.json` is the canonical machine-readable policy register for the local contract. The roster and acknowledgment files must still come from real HR/employee-notice exports. The resulting JSON must be retained with the business-trial readiness evidence. Local unit tests prove the contract and privacy boundaries; they are not a substitute for real HR/legal publication, employee notice, or acknowledgment.
