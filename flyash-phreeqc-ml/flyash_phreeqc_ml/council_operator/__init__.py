"""Controlled cross-computer AI Council operator.

The operator is a trusted-host control plane.  Model roles remain confined to
the separately installed AI Council and never receive a writable WPI checkout.
"""

from .contracts import OPERATOR_VERSION, OperatorConfig, ProjectPolicy, TaskRequest
from .operator import CouncilOperator

__all__ = [
    "CouncilOperator",
    "OPERATOR_VERSION",
    "OperatorConfig",
    "ProjectPolicy",
    "TaskRequest",
]
