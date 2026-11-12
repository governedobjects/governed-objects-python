# Conformance adapter notes (first-party adapters)

NON-NORMATIVE test infrastructure. These notes describe the first-party Python and Go adapters for the Core conformance suite. They are adapter documentation, not part of the suite.

## Channels

Each governed-operation response has two separate channels:

| Channel | Python | Go | Used for |
| --- | --- | --- | --- |
| `caller_result` | `CommandResult.model_dump(mode="json")`, or the import result or refusal | `core.Outcome` and the returned `*store.ObjectRecord` through `encoding/json`, or the import result or refusal | Disclosure, invariance and exposed-version checks only |
| `semantic_view` (`channel: observer`) | `get_object(raw=True)`, `canonical_relationships`, `accepted_history` | `Runtime.Inspect` (an unaudited deep copy) | Canonical effects, protected-value sets and lineage |

The observer channel is administrative. The referee never treats it as requester-visible.

`native_request` (Python only) is the exact command dataclass passed to `execute()`. Its values are requester-supplied content under `AUTH-DISCLOSE-001`.

## What the adapters do not do

- They do not remove, blank, rename or add any field of a runtime return value.
- They do not propose, invent or rewrite Object Versions. Tokens are passed through as decimal integers. A non-integer `expected_version` is a request error, not coerced.
- They do not classify from reason strings, deny reasons or validation checks:
  - Python uses only `CommandResult.outcome_category`, and ALLOW → ACCEPTED. A non-accepted result the runtime left uncategorized is reported as `UNCLASSIFIED` (it executed, and carries no class).
  - Go maps `Outcome.Kind` one-to-one.
- They do not convert invalid or refused results into denials. Import refusals map to AUTHORIZATION_DENIED only for the runtime's own authorization code; every other refusal is REFUSED.
- They do not dedupe relationships, suppress history entries or reorder history.
- They do not synthesize relationship additions. Creation with relationships is passed to Python's `CreateObject(relationships=...)`. Go's creation API carries none, so Go returns UNMAPPABLE.
- They do not coerce values. Go's portable-type API takes `map[string]string`, so a non-string value is a request error.
- They do not synthesize operations a runtime lacks. Go has no clock hook and no detach, so it returns UNMAPPABLE for those; Go declares and implements replay. Python has no clock hook for accepted history, so it returns UNMAPPABLE.

## Fixture mapping (suite-owned, declared, not behavioral)

The fixture types, actors and fixture rules are defined by the suite (`fixtures/fixtures.json` in the suite) and sent to the adapter in `define_fixture_types`. The adapters implement the received definitions through each runtime's public API and keep no fixture type, actor or rule of their own.

- Python derives the runtime's type definitions, the fixture type adapter's rules and the actor capabilities from the received definition. A rejection by a fixture rule declares itself a domain rejection (`AdapterFailure.INVALID`) where the runtime's adapter contract has that field. The adapter's outcome class still comes only from the runtime's `outcome_category`.
- Go builds portable types through the public `Runtime.RegisterType` from the received definition and maps the abstract permissions to its capabilities.
- A mutation of a work-item changes a title only; no scenario sends another work-item change. Portable-record mutations are passed whole.
- The `reopen` action (a request to transition to draft) is declared and permitted in no state.
- Both adapters keep the received definitions across `reset` and re-apply them on `restart`. `restart{revoke}` rebuilds the runtime over the same store without the revoked grants.

## Why neither adapter can manufacture a PASS

1. **Disclosure** (CS-2, CS-2R, CS-3). The referee scans the raw `caller_result` for protected values taken from the observer (current and historical tokens, state values, relationship types and targets, and restored history post-images).
   - The adapters return the runtime value whole, so they cannot hide a leaked field.
   - An adapter that blanked fields would violate the rules above; the adapter source is small and reviewable.
2. **Invariance** (clause 2). The referee compares normalized `caller_result` values and outcome classes across token, state and relationship variants. Only requester-supplied and volatile values are masked.
   - The Python `native_request` could mask values only by echoing them into the request passed to the runtime.
   - The native request is reported verbatim, so that would be visible.
3. **Outcome classes.** These come from the runtime's own classification fields, with no inference. Where the runtime supplies no class, the scenario sees `UNCLASSIFIED` and fails; it does not pass on a guess.
4. **Canonical effects and lineage.**
   - These use each runtime's administrative read of its own store, taken after the step.
   - Exposed Object Versions are collected from `caller_result` independently of the observer and must be revisions in the observed history.
   - History is compared as returned. The adapters neither sort nor filter it.
5. **Gating.** UNMAPPABLE is accepted as NOT EXECUTABLE only on steps a scenario marks `allow_unmappable`. Setup failures are accepted as NOT EXECUTABLE only on steps marked `precondition_outcomes`. Everywhere else it is an ADAPTER-ERROR, which blocks the claim.
   - Claim-based NOT APPLICABLE triggers read the adapter's declared capabilities. Those declarations are listed in the provenance record for review.
6. **Declaration.** The adapter's name, version and claims are self-declared in `capabilities`; the runner records them as such and never inspects the implementation or how it was built.
