"""Project-root conftest.

`pytest_plugins` must live at the rootdir conftest (pytest deprecation),
so we load `pytest_homeassistant_custom_component` here. The plugin's
fixtures are opt-in (e.g. `hass`, `recorder_mock`), so tests that don't
request them aren't slowed down materially beyond the plugin's import
cost.
"""

from __future__ import annotations

pytest_plugins = ["pytest_homeassistant_custom_component"]
