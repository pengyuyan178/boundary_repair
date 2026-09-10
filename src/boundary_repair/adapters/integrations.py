"""Stable compatibility imports; implementations live in small single-responsibility modules."""
from boundary_repair.adapters.logic import LogicAdapter
from boundary_repair.adapters.model import FrozenModelAdapter
from boundary_repair.adapters.program import ProgramAdapter
from boundary_repair.adapters.workspace import DockerWorkspaceAdapter, GitArchiveWorkspaceAdapter

__all__ = ['LogicAdapter', 'FrozenModelAdapter', 'ProgramAdapter',
           'DockerWorkspaceAdapter', 'GitArchiveWorkspaceAdapter']
