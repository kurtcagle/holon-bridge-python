"""HolonBridge application factory.

Run with::

    python -m holonbridge.server
    # or
    uvicorn holonbridge.server:app --host 127.0.0.1 --port 3031
"""

from __future__ import annotations

import contextlib
import os
from typing import AsyncIterator

from fastapi import FastAPI

from .config import BankStore, Settings, get_settings
from .fuseki import FusekiClient
from .cache import RegistryCache
from .conn import resolve_conn
from .persona_state import PersonaStore
from .routes import (
    banks,
    dataset_admin,
    events,
    fluent,
    graphs,
    holon_routes,
    identity_admin,
    named_queries,
    named_rules,
    persona,
    pipeline,
    projection,
    scheduler as scheduler_routes,
    sparql,
    triggers,
    whoami,
)
from .scheduler import AnthropicProposer, Scheduler

__version__ = "0.1.0"


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @contextlib.asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        application.state.settings = settings
        application.state.banks = BankStore(settings)
        application.state.fuseki = FusekiClient(timeout=settings.request_timeout)
        application.state.registry = RegistryCache(ttl=settings.named_query_ttl)
        # Per-person persona session state -- see persona_state.py for why
        # this lives here (one per process, keyed by resolved Person)
        # rather than as MCP-layer module state like the dataset/bank
        # overrides.
        application.state.personas = PersonaStore()
        # Strong references to background runs. asyncio holds only a weak one,
        # so an unheld task can be collected mid-flight and the run just stops.
        application.state.tasks = set()

        application.state.scheduler = None
        if settings.scheduler_enabled:
            # The scheduler reads its configuration from the admin dataset, not
            # from whatever a caller has selected. Resolved once, at startup.
            admin = resolve_conn(
                settings=settings,
                banks=application.state.banks,
                override=settings.scheduler_dataset,
            )
            # A proposer is only attached when there is a key to use. With
            # none, LLMInvocation tasks record 'deferred' rather than failing.
            proposer = (
                AnthropicProposer(application.state.fuseki)
                if os.getenv("ANTHROPIC_API_KEY")
                else None
            )
            application.state.scheduler = Scheduler(
                application.state.fuseki,
                admin_conn=admin,
                tick_seconds=settings.scheduler_tick_seconds,
                proposer=proposer,
                max_firing_depth=settings.scheduler_max_firing_depth,
            )
            await application.state.scheduler.start()
        try:
            yield
        finally:
            if application.state.scheduler is not None:
                await application.state.scheduler.stop()
            for task in list(application.state.tasks):
                task.cancel()
            await application.state.fuseki.aclose()

    app = FastAPI(
        title="HolonBridge (Python)",
        version=__version__,
        summary="REST bridge between the Holon Graph Architecture pipeline and a Jena Fuseki backend.",
        lifespan=lifespan,
    )

    app.include_router(holon_routes.meta_router)
    app.include_router(dataset_admin.router)
    app.include_router(banks.router)
    app.include_router(sparql.router)
    app.include_router(graphs.router)
    app.include_router(holon_routes.router)
    app.include_router(events.router)
    app.include_router(fluent.router)
    app.include_router(identity_admin.router)
    app.include_router(named_queries.router)
    app.include_router(named_rules.router)
    app.include_router(triggers.router)
    app.include_router(pipeline.router)
    app.include_router(scheduler_routes.router)
    app.include_router(projection.router)
    app.include_router(persona.router)
    app.include_router(whoami.router)
    return app


app = create_app()


def main() -> None:  # pragma: no cover
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "holonbridge.server:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )


if __name__ == "__main__":  # pragma: no cover
    main()
