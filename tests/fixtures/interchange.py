# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Plain synthetic governance requirements owned by generic runtime tests.

These are explicit test inputs, not a runtime default or experiment-driver import.
"""

STANDARD_REQUIREMENTS = {
    "read": ["interchange.read"],
    "create": ["interchange.create"],
    "state": ["interchange.state"],
    "lifecycle": ["interchange.lifecycle"],
    "relationship_add": ["interchange.relationship.add"],
    "relationship_remove": ["interchange.relationship.remove"],
}

GRANT_CAPABILITIES = {
    "read": ["interchange.read"],
    "create": ["interchange.create"],
    "state and lifecycle actions": ["interchange.state", "interchange.lifecycle"],
    "relationship add": ["interchange.relationship.add"],
    "relationship remove": ["interchange.relationship.remove"],
    "import": ["interchange.import"],
}
