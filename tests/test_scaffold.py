"""Smoke test: the package imports and the layout is installed."""


def test_package_imports():
    import strata

    assert strata is not None
