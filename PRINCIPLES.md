# Project Principles

## Purpose

Governed Object systems explore how organizations can retain the durable **identity**, **meaning**, **accepted state**, **relationships**, **governance**, and **history** of their organizational assets and business entities independently of the applications, infrastructure providers, external SaaS systems, and intelligence systems used to operate them.

The objective is not merely portability of bytes or easier application development. The deeper objective is **business sovereignty**: the organization should be able to continue operating its business representation through replaceable implementations.

## Business Sovereignty

No organization should have to depend on a particular software vendor — including the creator or maintainer of this project — to retain the identity, meaning, accepted state, relationships, governance, and history of its own business.

Governed Object systems therefore seek to make applications, infrastructure providers, external SaaS systems, and intelligence providers **replaceable** without requiring the organization to reconstruct the business itself.

The project's creator or primary commercial implementation should not become a mandatory dependency for organizations to retain or interpret their own canonical business state. Future specification and conformance work should make multiple independent implementations possible.

## Architectural Litmus Test

If a particular application, SaaS provider, AI provider, runtime implementation, or commercial company disappeared, what part of the organization's business identity, meaning, accepted state, governance, relationships, or history would be lost?

The project should continually seek to reduce that answer.

---

## Derived Principles

### 1. Organization-Owned Canonical Identity

Durable canonical identity must not depend on an external provider's identifier.

Example:

```text
reservation-701          ← canonical identity (organization-owned)
Provider A external_id = A-991
Provider B external_id = B-774
```

Provider identifiers are bindings and projections, not canonical identity.

### 2. Accepted State Is Organization-Owned

External systems may report observations. They must not silently redefine accepted canonical business state.

```text
external observation  ≠  accepted canonical state
```

Accepted changes must enter through explicit authority and admission semantics.

### 3. Accepted Business History Should Survive Vendor Replacement

Where externally originated business changes are observable and accepted, they should become organization-owned canonical history. Accepted business history should survive vendor replacement without requiring the organization to reconstruct that history from the vendor alone.

**Known limit:** facts that were never observable cannot be reconstructed magically.

### 4. Applications Operate Business State; They Do Not Own It

Applications are replaceable surfaces and operators around durable canonical state. Replacing an application should not inherently require replacing canonical identity, business history, relationships, policy, or accepted state.

### 5. Providers Are Replaceable Operational Implementations

External SaaS and provider systems may hold operational projections or observations of business state. They should not be definitionally equivalent to the business itself.

Where supported:

```text
canonical state  →  provider projection
```

provider replacement should be possible without business migration.

### 6. Intelligence Is Replaceable and Non-Authoritative

Humans, deterministic agents, and probabilistic models may request actions. AI reasoning must not become the source of identity, authority, policy, or canonical truth. The governed substrate decides what an actor may know and do.

### 7. Authority Must Not Expand Downstream

Applications, agents, mandates, and delegated actors may narrow authority. They must not silently increase authority beyond their upstream source. Delegation should remain monotonic in the narrowing direction. Current runtime behavior demonstrates this pattern; specific depth limits or storage mechanisms are implementation choices, not founding principles.

### 8. Canonical Meaning Survives Projection Gaps

If a connected provider cannot represent a canonical field or concept, the organization must not lose that meaning merely to accommodate the provider.

Example:

```text
Canonical Reservation: requested_seating = patio
Provider B: does not support requested_seating
```

The canonical value remains valid organization-owned state.

### 9. Portability Means More Than Exporting Bytes

**Data export** is not the same as **business portability**.

Business portability may require preservation of identity, semantics, relationships, accepted state, governance, lifecycle, and history. A CSV export alone does not satisfy the project's sovereignty objective.

```text
Owning data bytes  ≠  Owning the durable digital representation of the business.
```

### 10. Implementations Must Be Replaceable Too

A Governed Object architecture should not solve vendor lock-in by creating a new mandatory vendor lock-in at the Governed Object runtime layer.

Future specification and conformance work should make it possible for multiple independent implementations to support the same portable semantics. This capability does not yet exist as a formal specification; it is a design requirement for specification extraction.

### 11. Conformance Must Not Require a Proprietary Service

A future conforming implementation should not have to call a service controlled by one company merely to preserve or interpret canonical business state. Commercial implementations and managed services may exist and compete. Portable semantics must not depend on one of them.

**Conformance must be testable without permission from, dependence on, or access to a particular commercial provider.**

### 12. Business Sovereignty Is a Design Constraint, Not a Feature

These principles should be used when evaluating future architecture changes, provider integrations, specification proposals, commercial features, and governance proposals.

If a proposed capability increases dependence on one implementation at the expense of business portability, the tradeoff must be made explicit.

### 13. Evolution Must Preserve Organizational Control

Specification, schema, and type evolution should not require organizations to surrender canonical identity, accepted history, or semantic meaning merely to upgrade runtimes, applications, or business-type definitions.

Migrations and conversions should be explicit, inspectable, and portable rather than dependent on one implementation or proprietary service.

A newer implementation may introduce new capabilities, but existing organization-owned business meaning should not become inaccessible solely because one runtime or vendor controls the upgrade path.

---

## Relationship to Current Evidence

These principles describe **design intent** and **evaluation criteria**. They do not claim that every principle is fully realized today.

Current demonstrated capabilities (so far) include governed synchronization, canonical authority for supported providers, provider replacement without canonical identity migration, and replaceable applications/agents around stable canonical state.

---

## Further Reading

- [Governance](GOVERNANCE.md) — current project governance (experimental stage)
- [License](LICENSE) and [LICENSING.md](LICENSING.md) — license terms and companion notices
