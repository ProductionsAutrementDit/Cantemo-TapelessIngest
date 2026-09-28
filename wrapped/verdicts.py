"""One verdict per item, assigned by ``plan``.

Only WRITABLE verdicts are ever acted on by ``apply``. ERROR means the
item could not be classified this run (P5 or Vidispine did not answer);
the next ``plan`` classifies it again.
"""

READY = "ready"
ALREADY_MIGRATED = "already-migrated"
ORIGINALS_MISSING = "originals-missing"
SPANNED = "spanned"
UNEXPECTED = "unexpected"
ERROR = "error"

WRITABLE = (READY, ALREADY_MIGRATED)
ALL = (READY, ALREADY_MIGRATED, ORIGINALS_MISSING, SPANNED, UNEXPECTED, ERROR)
