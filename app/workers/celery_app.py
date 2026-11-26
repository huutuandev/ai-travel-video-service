from uuid import UUID

from celery import Celery
from celery.signals import worker_process_init

from app.core.config import settings
from app.core.db import engine
from app.core.logging import configure_logging
from app.services.execution import dispatch_due_jobs, execute_job

config = settings()
configure_logging()
celery = Celery("travel_video", broker=config.redis_url.get_secret_value())
celery.conf.update(
    task_ignore_result=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    worker_concurrency=config.worker_concurrency,
    task_soft_time_limit=config.task_soft_timeout,
    task_time_limit=config.task_hard_timeout,
    broker_connection_retry_on_startup=True,
    broker_connection_timeout=3,
    broker_transport_options={
        "visibility_timeout": config.redispatch_after,
        "socket_connect_timeout": 3,
        "socket_timeout": 3,
    },
    beat_schedule={
        "dispatch-persisted-video-jobs": {
            "task": "video.dispatch",
            "schedule": config.dispatch_interval,
            "options": {"expires": config.dispatch_interval},
        }
    },
)


@worker_process_init.connect
def reset_database_pool(**_):
    if engine.cache_info().currsize:
        engine().dispose(close=False)


@celery.task(name="video.execute")
def execute(job_id: str):
    execute_job(UUID(job_id))


@celery.task(name="video.dispatch")
def dispatch():
    dispatch_due_jobs(lambda job_id: execute.apply_async(args=[job_id], retry=False))

