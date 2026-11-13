# Governed Objects Python runtime

Governed Objects is a policy-aware primitive for organizational assets: every
object has a declarative type, a canonical state, and an accepted-history
log, and every create/read/action goes through an explicit capability check
before it is accepted. This package is the Python reference runtime. See the
[specification](https://github.com/governedobjects/governed-objects-spec) and
the [conformance suite](https://github.com/governedobjects/governed-objects-conformance)
for the normative rules this runtime implements and tests against.

## Status

Experimental, version 0.2.1, `0.x`. This is not a production product,
conformance certification, or interoperability approval. It targets
specification revision `draft2.2` and self-reports 76/0/2/0 (pass/not
applicable/not executable/fail) against Core conformance suite 0.1.0 for that revision;
see the conformance adapter below to reproduce that result yourself. The
suite's own claim text for that run:

> governed-objects-python 0.2.1 passes the Governed Objects Conformance Suite
> 0.1.0 for v0alpha1 draft2.2 Core (self-attested; not certified). Not
> executed by suite 0.1.0: D22-CON-001, D22-CS4-003. Conditional capabilities
> claimed: create_with_relationships, governance_reconfiguration,
> mutable_objects, relationship_add, relationship_remove, replay, restart,
> restoration, semantic_portability_view, type_version_exposed,
> unconditional_mutation.

## Install

```sh
pip install governed-objects
```

### From a source checkout

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install .
```

Requires Python >=3.11. The distribution name is `governed-objects`; the
import package is `governed_objects`. The version is defined once, in
`pyproject.toml`, and exposed as `governed_objects.__version__`.

## Example

Defining a type, creating an actor and an object, and performing one accepted
and one denied operation:

```python
from pathlib import Path

from governed_objects.applications.registry import ApplicationRegistry
from governed_objects.domain.command import CreateObject
from governed_objects.domain.models import Actor, ActorType
from governed_objects.persistence.sqlite import SQLiteStore
from governed_objects.providers.registry import ProviderRegistry
from governed_objects.runtime.runtime import GovernedObjectRuntime
from governed_objects.types.extensions import TypeExtensionRegistry
from governed_objects.types.registry import TypeRegistry
from governed_objects.types.schema import FieldSchema, ObjectTypeDefinition

types = TypeRegistry()
types.register(ObjectTypeDefinition(
    kind="example.widget", initial_state="active",
    states={"active": {}},
    fields={"label": FieldSchema(type="string", required=True)},
    actions={"create": {"requires": ["widget.create"]}},
    read_requires=["widget.read"],
))

store = SQLiteStore(Path(":memory:"))
runtime = GovernedObjectRuntime.configured(
    store=store, type_registry=types, adapters=[],
    extension_registry=TypeExtensionRegistry(), providers=ProviderRegistry(),
    application_registry=ApplicationRegistry(),
)

writer = Actor(id="writer", type=ActorType.SERVICE, capabilities=["widget.create", "widget.read"])
result = runtime.execute(CreateObject("w-1", "example.widget", {"label": "first"}), actor=writer)
print("accepted create:", result.decision, result.object_version)
# accepted create: ALLOW 1

stranger = Actor(id="stranger", type=ActorType.SERVICE)
result = runtime.execute(CreateObject("w-2", "example.widget", {"label": "second"}), actor=stranger)
print("denied create:", result.decision, result.deny_reason)
# denied create: DENY ACTOR_CAPABILITY_MISSING

store.close()
```

A type opens an action or a read to every actor only with an explicit
`open: true` marker on the action (or `read_open: true` on the type) — an
action or read with no declared capabilities and no such marker denies every
actor, as above.

## Running the tests and the conformance adapter

```sh
pip install '.[dev]'
pytest
```

The full test run takes several minutes. `uv.lock` is provided for `uv`
users (`uv sync --extra dev`); it is optional.

The conformance adapter under `conformance-adapter/` speaks the suite's
adapter protocol on stdin/stdout. The runner is in the
[`governed-objects-conformance`](https://github.com/governedobjects/governed-objects-conformance)
repository. Run these commands from the root of that repository, with the
adapter path pointing into this checkout:

```sh
python -m runner --adapter-command "path/to/governed-objects-python/conformance-adapter/launch.sh" --output-dir results-python
python -m runner --verify-bundle results-python
```

The adapter runs the source in this checkout; the interpreter only needs
`pydantic` and `pyyaml`. Set `CONFORMANCE_PYTHON` to an interpreter that has
them if it differs from `python3` on `PATH`. An activated virtual environment
from the install step above already satisfies this.

Tested on Linux. macOS is expected to work but is untested. On Windows, use
WSL. The adapters are `bash` scripts.

## Known limits

- Experimental, `0.x`: no production, conformance-certification or
  interoperability claim.
- The following public methods change or read canonical state without a
  policy check and are for trusted integration code, not untrusted callers:
  `resolve_review`, `replay`, `detach_provider`, `replace_provider`,
  `apply_repair`, `ingest_observation`, registration, `get_object`,
  `history`, `canonical_relationships`. Capabilities for every policy check
  are taken from the `Actor` object the caller passes in; the runtime does
  not authenticate actors itself.
- `governed_objects.agent.session` and `governed_objects.llm` are shipped for
  experimental optional use, not a model-provider integration or a default
  service; they accept a caller-supplied `ReasoningModel` and install no
  provider SDK or live model call.
- `governed_objects.interchange` (artifact export/import, PRC) is a
  provisional, representation-limited, non-normative experiment, not an
  official wire standard.
- The SQLite store serializes operations on one store instance with a
  per-instance connection guard, and `create`/`add_relationship`/
  `remove_relationship`/transition commands re-check the expected Object
  Version and write state plus history in one transaction (two connections
  to the same file never both commit against the same prior version). This
  does not cover multi-store or distributed-transaction guarantees, and
  provider/adapter I/O runs outside the transaction.
- Query/traversal/delegation/provider/agent features and the conformance
  adapter are implementation-specific; this is not a cross-runtime wire
  protocol.

## License

This runtime is licensed under Apache 2.0; see [LICENSE](https://github.com/governedobjects/governed-objects-python/blob/main/LICENSE), [NOTICE](https://github.com/governedobjects/governed-objects-python/blob/main/NOTICE) and
[LICENSING.md](https://github.com/governedobjects/governed-objects-python/blob/main/LICENSING.md). Contribution terms are in [CONTRIBUTING.md](https://github.com/governedobjects/governed-objects-python/blob/main/CONTRIBUTING.md).
Report security issues as described in [SECURITY.md](https://github.com/governedobjects/governed-objects-python/blob/main/SECURITY.md).
