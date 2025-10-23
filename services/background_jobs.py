# services/background_jobs.py
from __future__ import annotations

import logging
import queue
import threading
from typing import Callable, Any

from config import db

logger = logging.getLogger(__name__)

_task_queue: "queue.Queue[tuple[Callable[..., Any], tuple[Any, ...], dict[str, Any]]]" = queue.Queue(maxsize=100)
_workers_started = False
_MAX_WORKERS = 5


def _worker(app):
    with app.app_context():
        while True:
            job = _task_queue.get()
            if job is None:
                _task_queue.task_done()
                break

            func, args, kwargs = job
            try:
                func(*args, **kwargs)
            except Exception:
                logger.exception("Background job failed")
            finally:
                try:
                    db.session.remove()
                except Exception:
                    logger.exception("Failed to cleanup DB session after background job")
                _task_queue.task_done()


def init_background_workers(app) -> None:
    global _workers_started
    if _workers_started:
        return

    for idx in range(_MAX_WORKERS):
        thread = threading.Thread(
            target=_worker,
            args=(app,),
            name=f"jobs-worker-{idx}",
            daemon=True,
        )
        thread.start()
    _workers_started = True
    logger.info("Background job workers started: %s", _MAX_WORKERS)


def shutdown_background_workers() -> None:
    if not _workers_started:
        return
    for _ in range(_MAX_WORKERS):
        _task_queue.put(None)


def enqueue_job(func: Callable[..., Any], *args: Any, **kwargs: Any) -> bool:
    try:
        _task_queue.put_nowait((func, args, kwargs))
        return True
    except queue.Full:
        logger.warning("Background job queue is full; rejecting task %s", getattr(func, "__name__", func))
        return False


def enqueue_intasend_webhook_job(*args: Any, **kwargs: Any) -> bool:
    from services.intasend_webhook_processor import process_intasend_webhook

    return enqueue_job(process_intasend_webhook, *args, **kwargs)
