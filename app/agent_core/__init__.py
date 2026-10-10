"""The single-purpose Doris query agent core.

The HTTP layer owns cookies and presentation.  This package owns the business
execution boundary so every query route follows the same lifecycle.
"""

from .orchestrator import AgentOrchestrator, CoreExecutionError

__all__ = ["AgentOrchestrator", "CoreExecutionError"]
