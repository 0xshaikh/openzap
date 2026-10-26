"""APScheduler wiring for cron-triggered workflows.

Jobs are (re)created from the workflow index whenever the server starts and
whenever ``POST /api/admin/reload`` is called, so editing a cron expression on
disk takes effect without restarting the process.
"""

from __future__ import annotations

import logging

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from openzap.config import Settings
from openzap.storage import Store
from openzap.triggers import run_scheduled_workflow

LOGGER = logging.getLogger("openzap.scheduler")

JOB_PREFIX = "openzap-workflow:"


def sync_scheduler(
    scheduler: BackgroundScheduler,
    store: Store,
    settings: Settings,
) -> list[str]:
    """Align the scheduler with the current workflow index.

    Removes jobs for workflows that no longer exist or are no longer scheduled,
    adds missing jobs and returns the names of the workflows now scheduled.
    """
    scheduled: list[str] = []
    existing = {
        job.id: job
        for job in scheduler.get_jobs()
        if job.id.startswith(JOB_PREFIX)
    }

    wanted: dict[str, str] = {}
    for row in store.list_workflows():
        if not row.get("enabled"):
            continue
        if row.get("trigger_type") != "schedule":
            continue
        wanted[row["name"]] = str(row.get("trigger_cron") or "")

    # Drop jobs that are no longer wanted.
    for job_id in list(existing):
        if job_id[len(JOB_PREFIX):] not in wanted:
            try:
                scheduler.remove_job(job_id)
            except JobLookupError:
                pass

    for name, cron in wanted.items():
        job_id = JOB_PREFIX + name
        if job_id in existing and existing[job_id].trigger == CronTrigger.from_crontab(cron):
            scheduled.append(name)
            continue
        if job_id in existing:
            try:
                scheduler.remove_job(job_id)
            except JobLookupError:
                pass
        try:
            scheduler.add_job(
                func=run_scheduled_workflow,
                trigger=CronTrigger.from_crontab(cron),
                args=[store, settings, name],
                id=job_id,
                name=f"openzap:{name}",
                coalesce=True,
                max_instances=1,
                replace_existing=True,
            )
            scheduled.append(name)
        except Exception as exc:  # noqa: BLE001 - keep serving other jobs
            LOGGER.error("Could not schedule workflow %r with cron %r: %s", name, cron, exc)

    return scheduled


def start_scheduler(store: Store, settings: Settings) -> BackgroundScheduler:
    """Start a background scheduler loaded with the current workflow index."""
    scheduler = BackgroundScheduler(daemon=True)
    scheduler.start()
    sync_scheduler(scheduler, store, settings)
    LOGGER.info("Scheduler started")
    return scheduler
