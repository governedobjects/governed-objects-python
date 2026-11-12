# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Same real synthetic admission controls under native and installed runtimes."""
from pathlib import Path
import runpy

import pytest

CONTRACT = runpy.run_path(str(Path(__file__).parents[1] / 'installed/observation_validity_contract.py'))


@pytest.mark.parametrize('name',list(CONTRACT['cases']()))
def test_observation_candidate_validity(tmp_path,name):
    CONTRACT['cases']()[name](tmp_path/'validity.db')
