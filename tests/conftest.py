"""Shared pytest fixtures for the gazdebordeaux integration tests.

Phase 3 will add `pytest_plugins = ["pytest_homeassistant_custom_component"]`
once HA-fixture-dependent tests are added (config flow, sensor entities).
For now the test suite is pure-Python so we keep this conftest minimal to
avoid pulling Home Assistant into the test environment.
"""

from __future__ import annotations
