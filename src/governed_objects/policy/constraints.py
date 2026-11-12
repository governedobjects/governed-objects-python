# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Optional required-constraint interpreters can only narrow base authority."""
from collections.abc import Callable

from governed_objects.domain.models import Actor, GovernedObject
from governed_objects.types.schema import ObjectTypeDefinition

ConstraintHandler = Callable[[Actor, str, GovernedObject | None, ObjectTypeDefinition], bool]
