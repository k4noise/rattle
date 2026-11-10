import rattle


def test_package_has_version() -> None:
    assert rattle.__version__ == "0.1.0"
