"""LangGraph-based orchestration (conditional routing, re-enterable entry point).

The linear stage loop in `services/orchestrator.py` remains for the demo page and for
backwards compatibility; this package is the flow that branches and resumes.

Two things live here that a reader might not expect:

* `checkpoint.py` -- a SQL-backed `BaseCheckpointSaver`. The runner persists a business
  cursor *and* per-node checkpoints; they answer different questions ("where did it stop"
  vs "what did it look like at each step") and neither replaces the domain tables.
* `acquisition.py` -- a second, smaller graph for the pre-inquiry flow. 获客 happens before
  an inquiry exists, so folding it into the quote graph would have meant an inquiry id that
  points at nothing. Two graphs, one harness, one policy table.
"""
from app.graph.acquisition import run_acquisition
from app.graph.builder import ENTRY_NODES, NODES, PipelineState, build_pipeline_graph
from app.graph.checkpoint import SqlCheckpointSaver, read_history
from app.graph.runner import GraphRun, latest_run, resume_graph, run_checkpoints, run_graph

__all__ = [
    'ENTRY_NODES',
    'NODES',
    'GraphRun',
    'PipelineState',
    'SqlCheckpointSaver',
    'build_pipeline_graph',
    'latest_run',
    'read_history',
    'resume_graph',
    'run_acquisition',
    'run_checkpoints',
    'run_graph',
]
