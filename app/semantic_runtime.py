"""Compatibility imports. Runtime implementation lives in agent_core."""

from .agent_core.runtime import SemanticQueryRuntime
from .agent_core.semantic_types import QueryClient, SemanticAnswer, SemanticRequest, SemanticRuntimeError

__all__ = ["SemanticQueryRuntime", "SemanticRuntimeError", "SemanticRequest", "SemanticAnswer", "QueryClient"]
