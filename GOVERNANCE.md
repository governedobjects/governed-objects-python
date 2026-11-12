# Governance

## Current Governance

The project is currently **founder-led** while the architecture and initial specification are being developed and validated.

There is no formal steering committee, contributor voting process, or certification body at this stage. Decisions about architecture direction, experimental phases, and documentation are made pragmatically to test whether the business sovereignty model can be implemented coherently.

This is intentional. Long-term governance has **not** been finalized.

## Decision Making During Experimental Stage

During this stage, decisions prioritize:

1. **Fidelity to [PRINCIPLES.md](PRINCIPLES.md)** — especially business sovereignty and replaceability
2. **Evidence from working experiments** — phases should demonstrate claims with tests and demos
3. **Honest limits** — unobserved vendor changes, projection gaps, and partial provider support must remain visible
4. **Separation of semantics from implementation**

Experimental phases may be superseded. Implementation details may change. Principles should remain the evaluation baseline.

## Relationship to PRINCIPLES.md

[PRINCIPLES.md](PRINCIPLES.md) is the project's architectural constitution during the experimental period.

Any future governance model should:

- Preserve the Business Sovereignty Principles
- Protect the portability of the specification
- Avoid giving any single implementation vendor unilateral control over what constitutes a conforming Governed Object system
- Enable conformance to be evaluated independently, without requiring permission from, dependence on, or access to a particular commercial provider

Governance processes should not be used to entrench one runtime, one hosted service, or one commercial vendor as mandatory for organizations to retain their canonical business state.

## Specification and Implementation Governance

Governance of a particular runtime, hosted service, distribution, or commercial implementation may differ from governance of the portable Governed Object specification.

A company or maintainer may legitimately control its own implementation, service, roadmap, and operational features. That control should not by itself confer permanent unilateral authority over portable specification semantics or technical conformance requirements.

Likewise, the existence of a founding or dominant implementation should not make that implementation a mandatory dependency for organizations to retain or interpret canonical business state.

**Future governance should avoid allowing financial scale, implementation market share, or contribution volume alone to confer unilateral control over portable specification semantics.**

Large implementers may contribute substantial engineering resources and should be able to participate meaningfully. However, contribution scale alone should not allow one organization to redefine portable semantics in ways that make independent implementations or smaller participants structurally dependent on it.

**Future governance changes should be evaluated against the same Business Sovereignty Principles used to evaluate technical architecture.**

A governance change that makes the portable specification materially dependent on a particular implementation or provider should therefore be treated as a sovereignty tradeoff and made explicit.

## Future Governance Goals

When the project moves beyond experimental validation, governance should aim to:

- Keep normative specification semantics independent of any one codebase
- Enable multiple conforming implementations to compete on quality, operations, and support
- Provide transparent processes for specification evolution
- Protect the specification and ecosystem from capture — particularly situations where "conformance" effectively requires dependence on one vendor

The specific structure (foundation, steering committee, etc.) remains to be decided. Trademark governance and certification/conformance governance are not decided here; see the specification repository's
[GOVERNANCE.md](https://github.com/governedobjects/governed-objects-spec/blob/main/GOVERNANCE.md) and
[TRADEMARKS.md](https://github.com/governedobjects/governed-objects-spec/blob/main/TRADEMARKS.md).

## What Is Not Yet Decided

The following are **explicitly deferred**:

- Foundation or nonprofit structure
- Technical steering committee structure and membership rules
- Formal contributor voting or election processes
- Long-term specification ownership and stewardship
- Licensing strategy beyond the terms stated in [LICENSE](LICENSE) and [LICENSING.md](LICENSING.md)
- Commercial model for any future implementation or service
- Relationship between reference implementation maintainers and specification editors

None of these should be inferred from the existence of this repository or the experimental runtime it contains.

## Contributing

Contributions: see [CONTRIBUTING.md](CONTRIBUTING.md).

## License

This runtime is licensed under Apache 2.0. See [LICENSE](LICENSE) and [LICENSING.md](LICENSING.md). Governance and principles documentation do not themselves grant or alter any license.
