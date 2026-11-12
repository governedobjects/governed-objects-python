# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Native execution of the same public installed derived-ownership scenarios."""
import importlib.util
from pathlib import Path
import pytest

_spec=importlib.util.spec_from_file_location('provider_relationship_ownership_contract',
    Path(__file__).parents[1]/'installed/provider_relationship_ownership_contract.py')
_contract=importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_contract)


@pytest.mark.parametrize('case_id',_contract.CASE_IDS)
def test_provider_relationship_ownership(case_id,tmp_path):
    _contract.run_case(case_id,tmp_path/'relationships.db')
