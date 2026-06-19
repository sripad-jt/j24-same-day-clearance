"""Temporal client + task-queue config. Mirrors j24-store-vision/shared/config.py.

Connects to Temporal Cloud (API-key auth) when TEMPORAL_API_KEY is set, otherwise
to a local dev server (`temporal server start-dev`). The pydantic data converter
lets us pass the shared/models.py types across the workflow/activity boundary.
"""
from __future__ import annotations

import os

from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter

TASK_QUEUE = os.getenv("TEMPORAL_TASK_QUEUE", "perishables-tq")


async def get_client() -> Client:
    address = os.getenv("TEMPORAL_ADDRESS", "localhost:7233")
    namespace = os.getenv("TEMPORAL_NAMESPACE", "default")
    api_key = os.getenv("TEMPORAL_API_KEY")

    if api_key:
        # Temporal Cloud — API-key auth over the Namespace Endpoint.
        return await Client.connect(
            address,
            namespace=namespace,
            api_key=api_key,
            tls=True,
            data_converter=pydantic_data_converter,
        )

    # Local dev server.
    return await Client.connect(
        address,
        namespace=namespace,
        data_converter=pydantic_data_converter,
    )
