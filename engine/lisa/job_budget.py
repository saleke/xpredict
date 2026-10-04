"""Cooperative wall-time budget shared by a job and its provider threads."""
from contextlib import contextmanager
from contextvars import ContextVar
import time

_deadline = ContextVar('lisa_job_deadline', default=None)


class JobDeadlineExceeded(TimeoutError):
    pass


def current_deadline():
    return _deadline.get()


def check_budget():
    deadline = current_deadline()
    if deadline is not None and time.monotonic() >= deadline:
        raise JobDeadlineExceeded('Scheduled job exhausted its execution budget')


def bounded_timeout(seconds):
    check_budget()
    deadline = current_deadline()
    return seconds if deadline is None else max(.001, min(seconds, deadline-time.monotonic()))


def budget_sleep(seconds):
    delay = bounded_timeout(seconds)
    time.sleep(delay)
    check_budget()


@contextmanager
def execution_budget(*, seconds=None, deadline=None):
    if seconds is not None:
        deadline = time.monotonic()+seconds
    previous = current_deadline()
    if previous is not None:
        deadline = previous if deadline is None else min(previous, deadline)
    token = _deadline.set(deadline)
    try:
        check_budget()
        yield
    finally:
        _deadline.reset(token)
