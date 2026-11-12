# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Real generic exact-binding controls shared with the standalone installed probe."""
from pathlib import Path
import runpy

import pytest

CONTRACT = runpy.run_path(str(Path(__file__).parents[1] / 'installed/exact_binding_contract.py'))


@pytest.mark.parametrize('name', list(CONTRACT['cases']()))
def test_exact_binding_admission(tmp_path, name):
    CONTRACT['cases']()[name](tmp_path / 'binding.db')
