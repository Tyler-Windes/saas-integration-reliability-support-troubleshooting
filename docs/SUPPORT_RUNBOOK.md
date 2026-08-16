# Integration support runbook and three synthetic incidents

> This runbook applies only to the fictional local account-to-support entitlement integration. It does not describe a real ticket, user, service-level agreement, client system, or production incident.

Use the correlation trail, event ledger, target attempt history, dead-letter state, and reconciliation output together. Do not diagnose from a single status message, edit generated evidence, expose the local API key, or make a direct target change merely to make reconciliation appear clean.

## Runbook topics

### 1. Unauthorized request

- **Symptom:** The integration request is rejected before transformation or target delivery, normally with an unauthorized response and a permanent rejection disposition.
- **Evidence to inspect:** Correlation identifier, authentication result, request route, rejection reason, target-call count, and the absence of a retry or dead-letter transition.
- **Likely classification:** Permanent authentication rejection caused by a missing or incorrect local test key, an incorrect header, or a caller using the wrong endpoint.
- **Diagnostic sequence:** Locate the correlation trail; confirm that authentication failed before contract validation; confirm zero target attempts; compare the caller's header name and local environment source with the documented placeholder without printing the secret; then verify that no downstream state changed.
- **Safe action:** Correct the local test configuration or caller header, keep the key outside source and logs, and repeat the synthetic request through the normal source path.
- **Escalation condition:** Escalate when a correctly configured caller is still rejected, the key appears in evidence, the target was called after failed authentication, or authentication behavior differs across clean local runs.
- **Closure evidence:** A correctly authenticated synthetic request reaches contract processing exactly once, the failed request remains traceable, no secret is exposed, and no unauthorized target write occurred.

### 2. Schema or required-field rejection

- **Symptom:** A source event is permanently rejected because a required field is absent, malformed, empty, or incompatible with the source contract.
- **Evidence to inspect:** Event snapshot, contract error, missing-field detail, correlation identifier, rejection disposition, attempt count, and target state for the affected account.
- **Likely classification:** Permanent source-contract failure; retrying the unchanged payload cannot succeed.
- **Diagnostic sequence:** Compare the event to the source schema; identify the first actionable field error; confirm that transformation and target delivery did not run; check that the rejection is specific rather than generic; and verify that the current target record is unchanged.
- **Safe action:** Correct the fictional source record at its authority, issue a contract-valid event through the normal path, and preserve the original rejection evidence.
- **Escalation condition:** Escalate when an invalid payload reaches the target, the rejection lacks a specific field reason, multiple fields fail inconsistently, or a contract-valid payload is rejected.
- **Closure evidence:** The invalid event remains permanently rejected with a specific reason, the corrected event follows the standard path, and the target reflects only a valid transformation.

### 3. Invalid enum

- **Symptom:** An event containing an unsupported account status, support tier, contract state, event type, or region is rejected without a target write.
- **Evidence to inspect:** Rejected field and value, allowed-value contract, correlation identifier, rejection disposition, target-call count, and target before/after state.
- **Likely classification:** Permanent semantic-contract failure, not a transient service condition.
- **Diagnostic sequence:** Identify the rejected value; compare it with the exact allowed set and capitalization; determine whether the source produced a new unsupported value or a mapping assumption is stale; confirm zero retries and zero target changes; then check other events for the same value.
- **Safe action:** Correct the fictional source value or follow the controlled contract-change process before resubmission. Do not silently coerce an unknown value.
- **Escalation condition:** Escalate when a legitimate new value requires a contract decision, the same invalid value appears repeatedly, or the integration coerces or delivers it despite rejection rules.
- **Closure evidence:** The unsupported value is retained in rejection evidence, the approved corrected value is processed predictably, and no undocumented mapping is introduced.

### 4. Duplicate event

- **Symptom:** A repeated event identity is ignored, with no second entitlement and no repeated target mutation.
- **Evidence to inspect:** Event identity, original and repeated correlation trails, idempotency reservation, duplicate disposition, target-call count, and target key count.
- **Likely classification:** Expected idempotency protection when the same event is delivered more than once.
- **Diagnostic sequence:** Confirm the event identities are identical; locate the original terminal disposition; verify the duplicate check occurred before transformation delivery; compare target state before and after the repeat; and confirm no additional entitlement or attempt was created.
- **Safe action:** Leave the duplicate ignored. Investigate the upstream repeat separately if it is unexpected; do not delete the original ledger entry or force another target write.
- **Escalation condition:** Escalate when a duplicate changes target state, creates another target key, triggers a new target attempt, or is confused with a legitimate newer-version event.
- **Closure evidence:** The repeated event is linked to the original processing history, target state is unchanged, and the account retains exactly one entitlement record.

### 5. Out-of-order event

- **Symptom:** An event with an older or equal version is ignored and does not replace newer target state.
- **Evidence to inspect:** Incoming version, current target source version, event timestamps, correlation trail, out-of-order disposition, target-call count, and target before/after values.
- **Likely classification:** Expected event-order protection rather than delivery failure.
- **Diagnostic sequence:** Compare numeric versions before relying on timestamps; confirm that the target already holds a newer or equal source version; verify that the ignored event did not call or mutate the target; and locate the newer event that established current state.
- **Safe action:** Preserve the ignored event as lineage evidence. If a business correction is needed, issue a genuinely newer contract-valid source event rather than replaying the stale event.
- **Escalation condition:** Escalate when an older version overwrites newer state, versions are missing or noncomparable, or source ordering repeatedly conflicts with documented ownership.
- **Closure evidence:** The latest valid version remains authoritative, the stale event is traceable as ignored, and reconciliation reflects the newer source state.

### 6. Transient retry

- **Symptom:** A target call receives a modeled rate-limit or service-unavailable response and is attempted again within the three-call cap.
- **Evidence to inspect:** Correlation identifier, response sequence, attempt numbers, retry classification, terminal disposition, target writes, and elapsed modeled retry sequence.
- **Likely classification:** Transient target failure eligible for bounded retry.
- **Diagnostic sequence:** Confirm the response is an eligible transient status; inspect attempts in order; verify that no more than three target calls occur; determine whether a later call succeeds; and confirm that a successful retry produces one target state, not one per attempt.
- **Safe action:** Allow the bounded retry sequence to finish. Do not launch a manual replay while an event is still active, and do not broaden retry eligibility to contract or authentication failures.
- **Escalation condition:** Escalate when the cap is exceeded, a nontransient error retries, attempt evidence is missing, or repeated transient failures suggest target instability.
- **Closure evidence:** Either one later attempt applies the event with complete lineage, or the third unsuccessful target call transitions the event once to dead letter.

### 7. Dead-letter review

- **Symptom:** An otherwise valid event remains unapplied after all three eligible target attempts and is retained for review.
- **Evidence to inspect:** Frozen event snapshot, original correlation identifier, all three attempt responses, final transient error, dead-letter timestamp, replay eligibility, source version, and current target state.
- **Likely classification:** Exhausted transient target failure; the payload is not presumed defective merely because delivery exhausted.
- **Diagnostic sequence:** Confirm authentication and schema checks passed; verify exactly three eligible failed target calls; confirm one dead-letter record exists; compare the event version with current target state; and determine whether the modeled target fault has cleared before considering replay.
- **Safe action:** Preserve the event and attempt history unchanged, correct the modeled dependency condition, and use only the controlled replay path when eligibility remains valid.
- **Escalation condition:** Escalate when the event is missing, duplicated, altered, no longer version-eligible, dead-lettered for a permanent error, or the target condition cannot be classified safely.
- **Closure evidence:** Review classification is documented, event and correlation lineage remain intact, and the item is either safely replayed or explicitly retained without an unauthorized target write.

### 8. Replay

- **Symptom:** A reviewed dead-letter event needs controlled redelivery after the modeled transient failure clears.
- **Evidence to inspect:** Dead-letter record, original event identity and correlation, replay eligibility decision, new linked correlation, replay attempt, terminal disposition, target key count, and reconciliation result.
- **Likely classification:** Controlled recovery action for an eligible exhausted transient failure.
- **Diagnostic sequence:** Verify the item is dead-lettered and not already resolved; confirm the target fault is cleared; compare its source version with current target state; initiate one replay; then verify linkage from the new correlation to the original event and dead-letter history.
- **Safe action:** Replay the preserved event without editing its business fields or identity. Stop if eligibility has changed; use a genuinely newer source event for a business correction.
- **Escalation condition:** Escalate when lineage is incomplete, replay creates a duplicate target record, an ineligible event is accepted, the target still fails, or the replay cannot reach a single terminal disposition.
- **Closure evidence:** The original event identity is preserved, the replay correlation is linked, one entitlement is applied, the dead-letter item is resolved once, and reconciliation returns the expected classification.

### 9. Reconciliation mismatch

- **Symptom:** Reconciliation reports a missing, stale, mismatched, or unexpected target record instead of a match.
- **Evidence to inspect:** Latest valid source record, deterministic expected transformation, complete actual target key set, mismatch fields, source version, last event identity, correlation trail, and relevant attempt history.
- **Likely classification:** State divergence, with the precise subtype determined by the reconciliation result rather than by a generic integration failure label.
- **Diagnostic sequence:** Freeze the mismatch evidence; identify the authoritative latest source version; recompute the expected mapping; compare every target field and key; trace the last applied event; and determine whether the cause is a missed delivery, stale version, deliberate test alteration, or unexpected target state.
- **Safe action:** Correct through a controlled synthetic repair derived from the latest valid source state. For this mismatch exercise, preserve the altered-state evidence, recompute the expected transformation, and apply the test-only repair path rather than inventing a higher-version event or silently masking the difference.
- **Escalation condition:** Escalate when the expected transformation is ambiguous, multiple accounts diverge, an unexplained target key exists, lineage is missing, or correction would require bypassing the integration.
- **Closure evidence:** The original mismatch remains inspectable, the repair retains the source version, last event, and correction correlation, a fresh full-key comparison is a match, and no unrelated target state changed.

### 10. Target-service unavailability

- **Symptom:** The target is unavailable or returns the modeled service-unavailable response while the source and integration-state services remain reachable.
- **Evidence to inspect:** Service health, correlation identifier, target response or connection evidence, attempt sequence, retry cap, terminal disposition, dead-letter state, and target before/after state.
- **Likely classification:** Transient target dependency failure until bounded attempts exhaust; after exhaustion, dead-letter review is required.
- **Diagnostic sequence:** Separate target unavailability from caller authentication or payload rejection; verify source and ledger availability; confirm eligible responses only; observe attempts up to the cap; and check whether any target write occurred before failure.
- **Safe action:** Restore the modeled local target dependency, let an active bounded retry finish, or review the dead-letter record before one controlled replay. Do not edit target data to simulate success.
- **Escalation condition:** Escalate when availability cannot be restored, the failure classification changes, the retry cap is violated, partial target state appears, or multiple events accumulate without review.
- **Closure evidence:** Target health is restored, each affected event has one unambiguous terminal state, exhausted items have complete dead-letter/replay lineage, and reconciliation accounts for every expected target key.

## Synthetic incident walkthroughs

### Incident 1 — Authentication failure without retry

- **Reported symptom:** A fictional entitlement update never reached the target and the caller received an unauthorized response.
- **Reproduction:** Submit a contract-valid synthetic account event with an incorrect local API key.
- **Evidence:** The correlation trail records authentication failure and permanent rejection; target-attempt count is zero; target state and dead-letter state are unchanged.
- **Root cause or classification:** Permanent authentication rejection caused by incorrect local caller configuration, not a target outage.
- **Disposition:** Correct the local test key source without exposing its value, then repeat the synthetic request through the normal authenticated path.
- **Verification:** The original rejection remains traceable, the corrected request reaches contract processing once, and no retry or target write is associated with the unauthorized attempt.
- **Knowledge/runbook link:** [Unauthorized request](#1-unauthorized-request).

### Incident 2 — Service-unavailable exhaustion followed by replay

- **Reported symptom:** A valid fictional entitlement event did not apply after repeated service-unavailable responses and appeared in dead-letter review.
- **Reproduction:** Configure the local target mock to return service unavailable for all three permitted calls, submit one valid event, then clear the modeled fault.
- **Evidence:** One event snapshot, one original correlation, three ordered failed attempts, one dead-letter record, and zero target writes exist before recovery.
- **Root cause or classification:** Exhausted transient target failure, eligible for controlled replay after service restoration.
- **Disposition:** Confirm version eligibility, preserve the dead-letter evidence, and invoke one replay using the same event identity with a new linked correlation.
- **Verification:** Replay applies one entitlement, creates no duplicate target key, resolves the dead-letter item once, retains both correlations, and restores the expected reconciliation result.
- **Knowledge/runbook link:** [Target-service unavailability](#10-target-service-unavailability), [dead-letter review](#7-dead-letter-review), and [replay](#8-replay).

### Incident 3 — Detected field mismatch and controlled synthetic repair

- **Reported symptom:** The target record exists, but reconciliation reports a field mismatch after a deliberate synthetic alteration.
- **Reproduction:** Apply a valid event, alter one mapped target field in the local test state, and run the complete reconciliation comparison.
- **Evidence:** The latest source record and expected transformation agree; the actual target differs in the named field; source version, last event, and correlation lineage identify the previously applied state.
- **Root cause or classification:** Synthetic target-state divergence classified as `FIELD_MISMATCH`, not source-contract failure.
- **Disposition:** Preserve the mismatch snapshot, derive the exact expected target state from the latest valid source record, and apply the explicit test-only repair path without inventing a new source event or treating the altered target as authoritative.
- **Verification:** The repair is traceable to source version `1`, last event `EVENT-0007`, and correction correlation `TRACE-0011`; a fresh full-key comparison is `MATCH`, and no unrelated account or target key changes.
- **Knowledge/runbook link:** [Reconciliation mismatch](#9-reconciliation-mismatch) and [out-of-order event](#5-out-of-order-event).
