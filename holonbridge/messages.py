"""``hb:Message`` — the status record for an asynchronous run.

Ingestion and pipeline runs return immediately with a message id; the caller
polls for the outcome. That only works if the record outlives the request, so
messages live in the graph (``urn:{dataset}:messages``) rather than in process
memory. A bridge restart mid-run leaves a message stuck in ``Running``, which
is honest — better than a status that vanishes.

Every literal written here goes through :func:`holonbridge.turtle.literal`.
Message detail carries error text and file paths, which is exactly the
material that breaks a hand-built Turtle string.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .conn import Conn
from .fuseki import FusekiClient
from .turtle import literal

HB = "https://w3id.org/holonbridge/"
XSD_DATETIME = "<http://www.w3.org/2001/XMLSchema#dateTime>"
XSD_INTEGER = "<http://www.w3.org/2001/XMLSchema#integer>"

MESSAGE_STATUSES = ("Received", "Running", "Completed", "Failed")
STAGE_STATUSES = ("Pending", "Running", "Completed", "Failed", "Deferred", "Skipped")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _local_name(iri: str) -> str:
    cut = max(iri.rfind("/"), iri.rfind("#"))
    return iri[cut + 1 :] if cut >= 0 else iri


@dataclass
class StageRecord:
    name: str
    transformer: str = ""
    status: str = "Pending"
    detail: str = ""
    triples_written: int = 0
    order: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "transformer": self.transformer,
            "status": self.status,
            "detail": self.detail,
            "triplesWritten": self.triples_written,
            "order": self.order,
        }


@dataclass
class Message:
    id: str
    status: str = "Received"
    pipeline: str = ""
    target_graph: str = ""
    received_at: str = field(default_factory=_now)
    started_at: str = ""
    completed_at: str = ""
    error: str = ""
    stages: list[StageRecord] = field(default_factory=list)

    @staticmethod
    def new_id() -> str:
        return uuid.uuid4().hex

    def iri(self, conn: Conn) -> str:
        return conn.scoped("messages", self.id)

    def as_dict(self) -> dict[str, Any]:
        return {
            "messageId": self.id,
            "status": self.status,
            "pipeline": self.pipeline,
            "targetGraph": self.target_graph,
            "receivedAt": self.received_at,
            "startedAt": self.started_at,
            "completedAt": self.completed_at,
            "error": self.error,
            "stages": [s.as_dict() for s in sorted(self.stages, key=lambda s: s.order)],
        }


class MessageStore:
    """Reads and writes message records in the graph."""

    def __init__(self, client: FusekiClient) -> None:
        self._client = client

    async def save(self, conn: Conn, message: Message) -> None:
        """Write the message, replacing any previous version of it.

        Delete-then-insert on the message's own subtree only, so two runs
        writing to the messages graph at once cannot clobber each other.
        """
        graph = conn.graph("messages")
        iri = message.iri(conn)

        await self._client.update(
            conn,
            f"""DELETE {{ GRAPH <{graph}> {{ <{iri}> ?p ?o . ?stage ?sp ?so . }} }}
WHERE {{
  GRAPH <{graph}> {{
    <{iri}> ?p ?o .
    OPTIONAL {{ <{iri}> <{HB}stage> ?stage . ?stage ?sp ?so . }}
  }}
}}""",
        )
        await self._client.update(conn, f"INSERT DATA {{ {self._turtle(conn, message)} }}")

    def _turtle(self, conn: Conn, message: Message) -> str:
        graph = conn.graph("messages")
        iri = message.iri(conn)

        lines = [
            f"  GRAPH <{graph}> {{",
            f"    <{iri}> a <{HB}Message> ;",
            f"      <{HB}messageId> {literal(message.id)} ;",
            f"      <{HB}status> {literal(message.status)} ;",
            f"      <{HB}receivedAt> {literal(message.received_at, datatype=XSD_DATETIME)} ;",
        ]
        if message.pipeline:
            lines.append(f"      <{HB}pipeline> {literal(message.pipeline)} ;")
        if message.target_graph:
            lines.append(f"      <{HB}targetGraph> {literal(message.target_graph)} ;")
        if message.started_at:
            lines.append(
                f"      <{HB}startedAt> {literal(message.started_at, datatype=XSD_DATETIME)} ;"
            )
        if message.completed_at:
            lines.append(
                f"      <{HB}completedAt> {literal(message.completed_at, datatype=XSD_DATETIME)} ;"
            )
        if message.error:
            lines.append(f"      <{HB}error> {literal(message.error)} ;")

        for index, stage in enumerate(message.stages):
            lines.append(f"      <{HB}stage> <{iri}:stage:{index}> ;")

        lines[-1] = lines[-1].rstrip(" ;") + " ."

        for index, stage in enumerate(message.stages):
            lines.extend(
                [
                    f"    <{iri}:stage:{index}> a <{HB}MessageStage> ;",
                    f"      <{HB}stageName> {literal(stage.name)} ;",
                    f"      <{HB}stageStatus> {literal(stage.status)} ;",
                    f"      <{HB}transformer> {literal(stage.transformer)} ;",
                    f"      <{HB}triplesWritten> {literal(str(stage.triples_written), datatype=XSD_INTEGER)} ;",
                    f"      <{HB}order> {literal(str(stage.order), datatype=XSD_INTEGER)} ;",
                    f"      <{HB}detail> {literal(stage.detail)} .",
                ]
            )

        lines.append("  }")
        return "\n".join(lines)

    async def get(self, conn: Conn, message_id: str) -> Message | None:
        graph = conn.graph("messages")
        iri = conn.scoped("messages", message_id)

        results = await self._client.select(
            conn,
            f"""SELECT ?p ?o ?stage ?sp ?so
WHERE {{
  GRAPH <{graph}> {{
    <{iri}> ?p ?o .
    OPTIONAL {{ <{iri}> <{HB}stage> ?stage . ?stage ?sp ?so . }}
  }}
}}""",
        )
        rows = results.get("results", {}).get("bindings", [])
        if not rows:
            return None

        props: dict[str, str] = {}
        stages: dict[str, dict[str, str]] = {}
        for row in rows:
            props.setdefault(_local_name(row["p"]["value"]), row["o"]["value"])
            if "stage" in row and "sp" in row:
                stages.setdefault(row["stage"]["value"], {})[
                    _local_name(row["sp"]["value"])
                ] = row["so"]["value"]

        return Message(
            id=props.get("messageId", message_id),
            status=props.get("status", "Received"),
            pipeline=props.get("pipeline", ""),
            target_graph=props.get("targetGraph", ""),
            received_at=props.get("receivedAt", ""),
            started_at=props.get("startedAt", ""),
            completed_at=props.get("completedAt", ""),
            error=props.get("error", ""),
            stages=[
                StageRecord(
                    name=s.get("stageName", ""),
                    transformer=s.get("transformer", ""),
                    status=s.get("stageStatus", "Pending"),
                    detail=s.get("detail", ""),
                    triples_written=int(s.get("triplesWritten", "0") or 0),
                    order=int(s.get("order", "0") or 0),
                )
                for s in stages.values()
            ],
        )

    async def recent(self, conn: Conn, limit: int = 20) -> list[dict[str, Any]]:
        graph = conn.graph("messages")
        results = await self._client.select(
            conn,
            f"""SELECT ?id ?status ?receivedAt ?pipeline
WHERE {{
  GRAPH <{graph}> {{
    ?m a <{HB}Message> ;
       <{HB}messageId> ?id ;
       <{HB}status> ?status ;
       <{HB}receivedAt> ?receivedAt .
    OPTIONAL {{ ?m <{HB}pipeline> ?pipeline }}
  }}
}}
ORDER BY DESC(?receivedAt)
LIMIT {int(limit)}""",
        )
        return [
            {
                "messageId": row["id"]["value"],
                "status": row["status"]["value"],
                "receivedAt": row["receivedAt"]["value"],
                "pipeline": row.get("pipeline", {}).get("value", ""),
            }
            for row in results.get("results", {}).get("bindings", [])
        ]


def mark_running(message: Message) -> None:
    message.status = "Running"
    message.started_at = _now()


def mark_completed(message: Message, *, failed: bool = False, error: str = "") -> None:
    message.status = "Failed" if failed else "Completed"
    message.completed_at = _now()
    if error:
        message.error = error
