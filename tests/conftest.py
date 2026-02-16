"""Root test conftest — ensure required environment variables are set."""

import os


def pytest_configure(config):
    """Set dummy values for cloud API env vars that dolphin.yaml references."""
    _defaults = {
        "KIMI_API_BASE": "https://api.example.com/v1",
        "KIMI_API_KEY": "test-dummy-key",
    }
    for key, value in _defaults.items():
        if key not in os.environ:
            os.environ[key] = value
