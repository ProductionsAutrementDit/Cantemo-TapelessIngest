"""Portal-free scan core (story 2.1).

This package's import-time surface is stdlib-only by contract (AD-1):
``scan.context`` must be importable in a bare interpreter with no Portal
stub installed. The single sanctioned ``portal.*`` import site for new
scan code is ``scan.adapters``.
"""
