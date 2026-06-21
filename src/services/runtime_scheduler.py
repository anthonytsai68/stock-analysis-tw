# -*- coding: utf-8 -*-
"""Runtime scheduler service for long-lived API/Web/Desktop processes."""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

from src.config import Config, get_config
from src.scheduler import Scheduler, normalize_schedule_times

logger = logging.getLogger(__name__)
CLI_SCHEDULER_OWNER_ENV = "DSA_CLI_SCHEDULER_OWNS_SCHEDULE"


class RuntimeSchedulerService:
    """Manage scheduled analysis inside the current API/Web/Desktop process."""

    def __init__(
        self,
        *,
        config_provider: Callable[[], Config] = get_config,
        task_runner: Optional[Callable[[Config, Any, Optional[List[str]]], Any]] = None,
        owns_schedule: Optional[bool] = None,
    ) -> None:
        self._config_provider = config_provider
        self._task_runner = task_runner
        if owns_schedule is None:
            owns_schedule = os.getenv(CLI_SCHEDULER_OWNER_ENV, "").strip().lower() not in {
                "1",
                "true",
                "yes",
                "on",
            }
        self._owns_schedule = owns_schedule
        self._lock = threading.RLock()
        self._run_lock = threading.Lock()
        self._scheduler: Optional[Scheduler] = None
        self._thread: Optional[threading.Thread] = None
        self._enabled = False
        self._last_run_at: Optional[str] = None
        self._last_success_at: Optional[str] = None
        self._last_error: Optional[str] = None
        self._last_skipped_at: Optional[str] = None
        self._last_skip_reason: Optional[str] = None

    @staticmethod
    def _make_schedule_args() -> SimpleNamespace:
        return SimpleNamespace(
            schedule=True,
            no_run_immediately=True,
            no_notify=False,
            no_market_review=False,
            dry_run=False,
            force_run=False,
            single_notify=False,
            no_context_snapshot=False,
            market_review=False,
            serve=False,
            serve_only=True,
            stocks=None,
            workers=None,
        )

    def _reload_config(self) -> Config:
        from main import _reload_runtime_config

        return _reload_runtime_config()

    def _run_analysis_once(self) -> None:
        if not self._run_lock.acquire(blocking=False):
            self._last_skipped_at = datetime.now().isoformat()
            self._last_skip_reason = "analysis_already_running"
            logger.warning("Runtime scheduler skipped run: analysis already running")
            return

        try:
            config = self._reload_config()
            runner = self._task_runner
            if runner is None:
                from main import run_scheduled_analysis

                runner = run_scheduled_analysis
            self._last_run_at = datetime.now().isoformat()
            result = runner(config, self._make_schedule_args(), None)
            if result is False:
                raise RuntimeError("runtime scheduled analysis reported failure")
            self._last_success_at = datetime.now().isoformat()
            self._last_error = None
        except Exception as exc:  # noqa: BLE001 - scheduled runs must not kill API process.
            self._last_error = str(exc)
            logger.exception("Runtime scheduled analysis failed: %s", exc)
        finally:
            self._run_lock.release()

    def _current_times(self) -> List[str]:
        config = self._config_provider()
        return normalize_schedule_times(
            getattr(config, "schedule_times", None),
            fallback_time=getattr(config, "schedule_time", "18:00"),
        )

    def start(self) -> None:
        with self._lock:
            if not self._owns_schedule:
                self.stop()
                return
            config = self._config_provider()
            if not getattr(config, "schedule_enabled", False):
                self.stop()
                return
            self.stop()
            times = normalize_schedule_times(
                getattr(config, "schedule_times", None),
                fallback_time=getattr(config, "schedule_time", "18:00"),
            )
            scheduler = Scheduler(
                schedule_time=getattr(config, "schedule_time", "18:00"),
                schedule_times=times,
                schedule_times_provider=self._current_times,
                register_signals=False,
            )
            scheduler.set_daily_task(self._run_analysis_once, run_immediately=False)
            thread = threading.Thread(
                target=scheduler.run,
                daemon=True,
                name="runtime-scheduler",
            )
            self._scheduler = scheduler
            self._thread = thread
            self._enabled = True
            thread.start()

    def stop(self) -> None:
        scheduler = self._scheduler
        if scheduler is not None:
            scheduler.stop()
        self._scheduler = None
        self._thread = None
        self._enabled = False

    def reconcile_from_config(self) -> None:
        if not self._owns_schedule:
            self.stop()
            return
        config = self._config_provider()
        if getattr(config, "schedule_enabled", False):
            self.start()
        else:
            self.stop()

    def run_now(self) -> Dict[str, Any]:
        worker = threading.Thread(
            target=self._run_analysis_once,
            daemon=True,
            name="runtime-scheduler-run-now",
        )
        worker.start()
        return {"accepted": True, "running": True}

    def status(self) -> Dict[str, Any]:
        scheduler = self._scheduler
        jobs = scheduler.schedule.get_jobs() if scheduler is not None else []
        next_run = None
        if jobs:
            next_run = min(job.next_run for job in jobs).isoformat()
        if scheduler is not None:
            schedule_times = list(getattr(scheduler, "schedule_times", []))
        else:
            try:
                schedule_times = self._current_times()
            except Exception:  # pragma: no cover - defensive status fallback
                schedule_times = []
        running = self._run_lock.locked()
        return {
            "enabled": self._enabled,
            "running": running,
            "schedule_times": schedule_times,
            "next_run_at": next_run,
            "last_run_at": self._last_run_at,
            "last_success_at": self._last_success_at,
            "last_error": self._last_error,
            "last_skipped_at": self._last_skipped_at,
            "last_skip_reason": self._last_skip_reason,
        }
