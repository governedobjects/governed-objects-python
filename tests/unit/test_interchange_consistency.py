# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Brafford Labs, LLC
"""Run the public installed interchange scenario set against native source."""
import pytest

from tests.installed.interchange_consistency_contract import CASE_IDS, run_case


@pytest.mark.parametrize('case', CASE_IDS)
def test_interchange_consistency(case, tmp_path):
    run_case(case, tmp_path/'case.db', {})
