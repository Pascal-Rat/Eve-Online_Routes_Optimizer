"""Assertions for optional responses whose presence is part of a test scenario."""


def present[T](value: T | None) -> T:
    assert value is not None
    return value
