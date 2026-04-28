"""Shared pytest fixtures for the gazdebordeaux integration tests.

The pure-Python API client tests at this level don't need Home Assistant.
Tests that *do* need HA fixtures live under `tests/integration/` and pull
in `pytest_homeassistant_custom_component` via that subdirectory's
conftest.
"""

from __future__ import annotations
