"""Persistence activities — mirror workflow state into the Postgres read-model."""
from __future__ import annotations

from temporalio import activity

from db import repo
from shared.models import MarkdownState


@activity.defn
async def persist_state(run_id: str, state: MarkdownState) -> None:
    repo.upsert_run_from_state(run_id, state)


@activity.defn
async def record_run_event(run_id: str, kind: str, message: str) -> None:
    repo.add_event(run_id, kind, message)


@activity.defn
async def persist_decision(run_id: str, decision: dict) -> None:
    repo.add_decision(run_id, decision)
