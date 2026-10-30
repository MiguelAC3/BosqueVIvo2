from bosquevivo.ai.agent.tools.base import (
    Role,
    Tool,
    ToolContext,
    ToolError,
    ToolOutput,
    ToolRegistry,
)
from bosquevivo.ai.agent.tools.catalog import CATALOG_TOOLS
from bosquevivo.ai.agent.tools.data import DATA_TOOLS
from bosquevivo.ai.agent.tools.extra import EXTRA_TOOLS


def default_registry() -> ToolRegistry:
    return ToolRegistry([*DATA_TOOLS, *CATALOG_TOOLS, *EXTRA_TOOLS])


__all__ = [
    "Role",
    "Tool",
    "ToolContext",
    "ToolError",
    "ToolOutput",
    "ToolRegistry",
    "default_registry",
]
