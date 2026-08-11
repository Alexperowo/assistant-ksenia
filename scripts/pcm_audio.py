"""PCM helpers for the project's pinned Python 3.12 runtime.

Python 3.12 deprecates :mod:`audioop`, but the module remains the reliable
standard-library implementation used by the current, explicitly pinned voice
runtime.  Keeping the compatibility suppression in one place makes a future
Python migration visible and prevents unrelated tests from hiding warnings.
"""

from __future__ import annotations

import warnings


with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore",
        message="'audioop' is deprecated.*",
        category=DeprecationWarning,
    )
    import audioop as _audioop


ratecv = _audioop.ratecv
rms = _audioop.rms
