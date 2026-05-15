"""UTC market calendar evaluation for live dispatch blocking.

Delegates to core.market.calendar — kept as thin wrapper for backward compatibility.
"""

from __future__ import annotations

# Re-export from canonical location
from core.market.calendar import (  # noqa: F401
    evaluate_pre_close,
    evaluate_utc_blackout,
    load_calendar,
)
