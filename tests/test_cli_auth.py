"""CLI registry 环境变量认证测试。"""

from __future__ import annotations

import pytest

from minisweharness.cli import _harbor_from_environment, _registry_credentials_from_environment


def test_registry_credentials_from_environment(monkeypatch):
    monkeypatch.setenv("HARBOR_USERNAME", "robot$h2")
    monkeypatch.setenv("HARBOR_PASSWORD", "secret")
    username, password = _registry_credentials_from_environment()
    assert (username, password) == ("robot$h2", "secret")
    client = _harbor_from_environment("harbor.private:12443")
    assert client._headers["Authorization"].startswith("Basic ")


def test_registry_credentials_require_pair(monkeypatch):
    monkeypatch.setenv("HARBOR_USERNAME", "robot$h2")
    monkeypatch.delenv("HARBOR_PASSWORD", raising=False)
    with pytest.raises(ValueError, match="provided together"):
        _registry_credentials_from_environment()
