"""Pytest fixtures for the HA-fixture-dependent integration tests."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _ha_test_setup(recorder_mock, enable_custom_integrations):
    """Order-sensitive setup for every integration test.

    `recorder_mock` MUST be set up before `hass` (the HA test plugin
    asserts that), and `enable_custom_integrations` is needed for HA's
    loader to discover this repo. Combining them in a single autouse
    fixture guarantees the order regardless of how individual tests
    declare their parameters.
    """
    yield
