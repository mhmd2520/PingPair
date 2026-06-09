"""Headless test logic — no Qt imports allowed in this package.

Keeping core/ Qt-free means it's trivially unit-testable with pytest and
can run in --check-prereqs mode without a display.
"""
