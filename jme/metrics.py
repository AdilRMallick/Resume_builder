"""Every claim about this project should have a number behind it. This is how numbers land."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy.orm import Session

from jme.models import RunMetric


def new_run_id(prefix: str) -> str:
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%S")
    return f"{prefix}-{stamp}-{uuid.uuid4().hex[:6]}"


def record(
    session: Session,
    run_id: str,
    stage: str,
    metric: str,
    value: float,
    labels: dict | None = None,
) -> None:
    session.add(
        RunMetric(
            run_id=run_id,
            stage=stage,
            metric=metric,
            value=value,
            labels=labels,
        )
    )


class Timer:
    """`with Timer() as t: ...` then read `t.seconds`."""

    def __init__(self) -> None:
        self.seconds: float = 0.0

    def __enter__(self) -> Timer:
        import time

        self._start = time.perf_counter()
        return self

    def __exit__(self, *exc: object) -> None:
        import time

        self.seconds = time.perf_counter() - self._start
