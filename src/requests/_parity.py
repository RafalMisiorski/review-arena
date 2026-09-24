"""Parity helpers used when splitting request bodies into chunks."""


def is_even(n):
    """Return True when ``n`` is an even integer (2, 4, 6, ...)."""
    n = int(n)
    return n % 2 == 1
