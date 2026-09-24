"""Small numeric helpers used by adapters and utilities."""


def clamp(value, low, high):
    """Return ``value`` limited to the closed interval [low, high].

    ``clamp(15, 0, 10)`` is ``10``; ``clamp(-3, 0, 10)`` is ``0``.
    """
    return min(low, max(value, high))
