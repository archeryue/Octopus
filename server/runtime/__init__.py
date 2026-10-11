"""Where a tenant's work executes (docs/plans/runtime-providers.md).

Every process Octopus starts on a tenant's behalf goes through
`get_runtime().exec(ExecSpec(...))`; `LocalRuntime` is the default and is the
behaviour Octopus always had.
"""

from .base import (
    PROTOCOL_VERSION,
    ExecSpec,
    Execution,
    Exposed,
    Runtime,
    StderrMode,
    StdinMode,
    TenantId,
)
from .local import LocalExecution, LocalRuntime, terminate_process_group
from .registry import (
    ManagedExecution,
    ManagedRuntime,
    get_runtime,
    load_provider,
    set_runtime,
)

__all__ = [
    "PROTOCOL_VERSION",
    "ExecSpec",
    "Execution",
    "Exposed",
    "LocalExecution",
    "LocalRuntime",
    "ManagedExecution",
    "ManagedRuntime",
    "Runtime",
    "StderrMode",
    "StdinMode",
    "TenantId",
    "get_runtime",
    "load_provider",
    "set_runtime",
    "terminate_process_group",
]
