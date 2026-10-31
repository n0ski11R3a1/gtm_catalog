import pytest

import config


def test_production_settings_are_required(monkeypatch):
    monkeypatch.setattr(config, "IS_PRODUCTION", True)
    monkeypatch.delenv("GTM_TEST_DEPLOYMENT_SECRET", raising=False)

    with pytest.raises(RuntimeError, match="GTM_TEST_DEPLOYMENT_SECRET"):
        config._app_setting("GTM_TEST_DEPLOYMENT_SECRET", "development-only")


def test_development_settings_keep_local_defaults(monkeypatch):
    monkeypatch.setattr(config, "IS_PRODUCTION", False)
    monkeypatch.delenv("GTM_TEST_DEPLOYMENT_SECRET", raising=False)

    assert config._app_setting("GTM_TEST_DEPLOYMENT_SECRET", "development-only") == "development-only"
