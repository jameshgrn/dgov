"""Async bridge for DagKernel — event-driven, zero-latency via pipe.

Follows sentrux principles:
- Events are source of truth
- Zero-latency signaling (no polling)
- Reader opens pipe FIRST, then writers are spawned
- Fast dispatch path with cached preflight and spans
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from dgov.dag_parser import DagDefinition, DagTaskSpec
from dgov.kernel import (
    DagKernel,
    DispatchTask,
    GovernorAction,
    InterruptGovernor,
    MergeTask,
    TaskDispatched,
    TaskDispatchFailed,
    TaskGovernorResumed,
    TaskMergeDone,
    TaskWaitDone,
)
from dgov.persistence import emit_event
from dgov.preflight import run_preflight_async
from dgov.spans import SpanContext, annotate, flush
from dgov.tmux import create_background_pane, send_prompt_via_buffer, set_pane_option

logger = logging.getLogger(__name__)
