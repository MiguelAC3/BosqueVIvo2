from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from bosquevivo.db.models import Job

MAX_ATTEMPTS = 3
# Datos personales que solo se necesitan mientras el trabajo está pendiente (Ley 1581).
SENSITIVE_FIELDS = ("phone", "text")


def scrub(job: Job) -> None:
    if any(name in job.payload for name in SENSITIVE_FIELDS):
        job.payload = {k: v for k, v in job.payload.items() if k not in SENSITIVE_FIELDS}


class JobQueue:
    def __init__(self, session: Session) -> None:
        self.session = session

    def enqueue(self, kind: str, payload: dict[str, Any]) -> Job:
        job = Job(kind=kind, payload=payload, status="queued")
        self.session.add(job)
        self.session.flush()
        return job

    def claim_next(self) -> Job | None:
        job = self.session.scalars(
            select(Job).where(Job.status == "queued").order_by(Job.id).limit(1)
        ).first()
        if job is None:
            return None
        job.status = "running"
        job.attempts += 1
        self.session.flush()
        return job

    def complete(self, job: Job, result: dict[str, Any] | None = None) -> None:
        job.status = "done"
        job.result = result
        job.error = None
        scrub(job)
        self.session.flush()

    def fail(self, job: Job, error: str) -> None:
        job.error = error
        job.status = "queued" if job.attempts < MAX_ATTEMPTS else "failed"
        if job.status == "failed":
            scrub(job)
        self.session.flush()

    def get(self, job_id: int) -> Job | None:
        return self.session.get(Job, job_id)
