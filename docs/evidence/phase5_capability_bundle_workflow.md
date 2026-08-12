# Phase 5 Capability Bundle and Second Workflow Evidence

Date: 2026-07-19

## Scope

This evidence covers P5-01 Capability bundles and a second readonly workflow.

## Implementation facts

- Added `CapabilityBundle` and `CapabilityBundleRegistry` in
  `athena/capabilities/bundles.py`.
- Added a static `kubernetes-readonly` bundle exposing the existing governed
  K8s readonly tool contracts and the `crashloop` plus `pod_pending` workflows.
- Added `PodPendingDiagnosisWorkflow`, a second bounded readonly workflow that
  diagnoses Pending pods using `k8s.pod.list`, `k8s.pod.get` and
  `k8s.events.list`.
- The new workflow implements the same policy interface consumed by
  `WorkflowRunner`; no runner core-loop changes are required.
- Terminal success requires event evidence with a recognized root cause and
  matching data origin.

## Acceptance evidence

Focused tests:

```text
D:\tmp\mjy_agent\venv\Scripts\python.exe -m pytest tests\test_capability_bundles.py tests\test_policy_workflow.py tests\test_tool_runtime.py -q
21 passed in 2.34s
```

## Rollback note

Remove `athena/capabilities` and `athena/agent/workflow/pod_pending.py`, then
remove the workflow exports. Existing CrashLoop workflow and ToolRuntime remain
unchanged because the runner core was not modified.
