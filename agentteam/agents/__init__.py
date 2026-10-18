"""Agent factory."""

from __future__ import annotations

from .analyst import BusinessAnalyst
from .architect import SolutionArchitect
from .base import Agent, TeamContext
from .catalog import BUILDER_ROLES, CORE_ROLES, ROLE_CATALOG, RoleSpec
from .developer import Developer
from .devops import DevOpsEngineer
from .manager import ProjectManager
from .quality import QAEngineer, SecurityEngineer
from .writer import TechnicalWriter

_CLASSES: dict[str, type[Agent]] = {
    "business_analyst": BusinessAnalyst,
    "project_manager": ProjectManager,
    "solution_architect": SolutionArchitect,
    "qa_engineer": QAEngineer,
    "security_engineer": SecurityEngineer,
    "devops_engineer": DevOpsEngineer,
    "technical_writer": TechnicalWriter,
}


def create_agent(role: str, ctx: TeamContext) -> Agent:
    spec = ROLE_CATALOG[role]
    cls = _CLASSES.get(role, Developer if spec.builder else Agent)
    return cls(spec, ctx)


__all__ = ["Agent", "TeamContext", "RoleSpec", "ROLE_CATALOG", "CORE_ROLES", "BUILDER_ROLES", "create_agent"]
