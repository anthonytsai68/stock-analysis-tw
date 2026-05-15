# -*- coding: utf-8 -*-
"""Tests for the DSA Extension Runtime MVP."""

from __future__ import annotations

from concurrent.futures import Future
import threading
import time
import unittest
from types import SimpleNamespace

from src.extensions import (
    ActionContext,
    ActionDefinition,
    ActionMode,
    ExtensionRegistry,
    ExtensionRuntime,
    create_builtin_extension_runtime,
)
from src.extensions.permissions import ActionPermissionGuard
from src.extensions.registry import PluginDefinition, PluginStatus
from src.extensions.tasks import ActionTaskRunner
from src.services.task_queue import AnalysisTaskQueue, TaskStatus


def _echo(payload, context):
    return {"payload": payload, "caller": context.caller, "dry_run": context.dry_run}


class StubTaskRunner:
    def __init__(self):
        self.calls = []

    def submit(self, *, action, payload, context, run_id, run_callable):
        self.calls.append({"action_id": action.action_id, "payload": payload, "caller": context.caller})
        return SimpleNamespace(task_id="task_123")


class ExtensionRuntimeTestCase(unittest.TestCase):
    def _runtime(self, action=None, *, guard=None, task_runner=None):
        registry = ExtensionRegistry()
        action = action or ActionDefinition("test.echo", "test", "Echo payload", _echo, timeout_seconds=1)
        registry.register_plugin(
            PluginDefinition(
                action.plugin_id,
                action.plugin_id,
                "Test plugin",
                status=PluginStatus.ENABLED,
            )
        )
        registry.register_action(action)
        return ExtensionRuntime(registry=registry, permission_guard=guard, task_runner=task_runner)

    def test_action_context_contract_fields(self):
        context = ActionContext.from_mapping(
            {
                "caller": "agent",
                "trace_id": "trace_test",
                "session_id": "session_test",
                "idempotency_key": "dedupe",
                "dry_run": True,
                "budget": {"timeout_seconds": 5, "max_llm_calls": 2, "max_items": 7},
                "context": {"market": "cn"},
                "requires_confirmation": True,
            }
        ).to_dict()

        self.assertEqual(context["caller"], "agent")
        self.assertEqual(context["trace_id"], "trace_test")
        self.assertEqual(context["session_id"], "session_test")
        self.assertEqual(context["idempotency_key"], "dedupe")
        self.assertTrue(context["dry_run"])
        self.assertEqual(context["budget"], {"timeout_seconds": 5.0, "max_llm_calls": 2, "max_items": 7})
        self.assertEqual(context["context"], {"market": "cn"})
        self.assertTrue(context["requires_confirmation"])

        parsed = ActionContext.from_mapping({"dry_run": "false", "requires_confirmation": "0"}).to_dict()
        self.assertFalse(parsed["dry_run"])
        self.assertFalse(parsed["requires_confirmation"])

    def test_execute_sync_action_success_and_hashes_input(self):
        result = self._runtime().execute_action(
            "test.echo",
            {"symbol": "600519"},
            {"caller": "web"},
            async_mode=False,
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.data["payload"], {"symbol": "600519"})
        self.assertEqual(result.data["caller"], "web")
        self.assertEqual(len(result.input_hash), 64)

    def test_execute_sync_rejects_non_mapping_payloads(self):
        for payload in ([], "", 0, 0.0):
            payload_type = type(payload).__name__
            result = self._runtime().execute_action("test.echo", payload, {"caller": "web"}, async_mode=False)

            self.assertFalse(result.ok)
            self.assertEqual(result.error.code, "invalid_input")
            self.assertEqual(result.error.message, "Action payload must be a mapping.")
            self.assertEqual(result.status, "failed")
            self.assertEqual(result.error.details["payload_type"], payload_type)

    def test_execute_sync_accepts_none_payload_as_empty_object(self):
        result = self._runtime().execute_action("test.echo", None, {"caller": "web"}, async_mode=False)
        self.assertTrue(result.ok)
        self.assertEqual(result.status, "completed")

    def test_omitted_budget_does_not_cap_action_timeout(self):
        action = ActionDefinition("test.long", "test", "Long action", _echo, timeout_seconds=300)
        runtime = self._runtime(action)
        context = ActionContext.from_mapping({})

        result = runtime.execute_action("test.long", context={})

        self.assertTrue(result.ok)
        self.assertEqual(context.budget.timeout_seconds, 0.0)
        self.assertEqual(runtime._timeout_seconds(action, context), 300)

    def test_structured_errors_for_missing_permission_depth_timeout_and_handler(self):
        missing = ExtensionRuntime().execute_action("missing.action")
        denied = self._runtime(guard=ActionPermissionGuard(allowed_actions={"other.action"})).execute_action("test.echo")
        depth = self._runtime(guard=ActionPermissionGuard(max_call_depth=1)).execute_action(
            "test.echo", context={"call_depth": 2}
        )

        def slow(payload, context):
            time.sleep(0.2)
            return {"done": True}

        timeout = self._runtime(ActionDefinition("test.slow", "test", "Slow", slow, timeout_seconds=0.01)).execute_action(
            "test.slow"
        )

        def bad(payload, context):
            raise RuntimeError("secret token should not be returned")

        handler = self._runtime(ActionDefinition("test.bad", "test", "Bad", bad)).execute_action("test.bad")

        self.assertEqual(missing.error.code, "action_not_found")
        self.assertEqual(denied.error.code, "permission_denied")
        self.assertEqual(depth.error.code, "call_depth_exceeded")
        self.assertEqual(timeout.error.code, "timeout")
        self.assertEqual(handler.error.code, "handler_error")
        self.assertNotIn("secret token", handler.error.message)

    def test_async_action_submits_to_task_runner(self):
        task_runner = StubTaskRunner()
        runtime = self._runtime(
            ActionDefinition("test.async_echo", "test", "Async echo", _echo, mode=ActionMode.ASYNC, subject_key="symbol"),
            task_runner=task_runner,
        )

        result = runtime.execute_action("test.async_echo", {"symbol": "600519"}, {"caller": "agent"})

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "accepted")
        self.assertEqual(result.task_id, "task_123")
        self.assertEqual(task_runner.calls[0], {"action_id": "test.async_echo", "payload": {"symbol": "600519"}, "caller": "agent"})

    def test_async_action_failure_marks_queue_task_failed(self):
        class SyncExecutor:
            def submit(self, func, *args):
                future = Future()
                try:
                    future.set_result(func(*args))
                except Exception as exc:  # pragma: no cover - the queue handles task failures
                    future.set_exception(exc)
                return future

        original_instance = AnalysisTaskQueue._instance
        AnalysisTaskQueue._instance = None
        try:
            queue = AnalysisTaskQueue(max_workers=1)
            queue._executor = SyncExecutor()

            def bad(payload, context):
                raise RuntimeError("secret token should not be returned")

            runtime = self._runtime(
                ActionDefinition(
                    "test.async_bad",
                    "test",
                    "Async bad",
                    bad,
                    mode=ActionMode.ASYNC,
                    subject_key="symbol",
                ),
                task_runner=ActionTaskRunner(queue_factory=lambda: queue),
            )

            accepted = runtime.execute_action("test.async_bad", {"symbol": "600519"}, {"caller": "agent"})
            task = queue.get_task(accepted.task_id)

            self.assertTrue(accepted.ok)
            self.assertEqual(accepted.status, "accepted")
            self.assertIsNotNone(task)
            self.assertEqual(task.status, TaskStatus.FAILED)
            self.assertEqual(task.error, "handler_error: Action handler failed.")
            self.assertEqual(task.action_id, "test.async_bad")
            self.assertEqual(task.run_id, accepted.run_id)
            self.assertEqual(task.caller, "agent")
        finally:
            AnalysisTaskQueue._instance = original_instance

    def test_disabled_plugin_action_is_rejected(self):
        registry = ExtensionRegistry()
        registry.register_plugin(
            PluginDefinition(
                "test",
                "Test plugin",
                "Test plugin",
                status=PluginStatus.DISABLED,
            )
        )
        self.assertEqual(registry.get_plugin("test").status, PluginStatus.DISABLED)
        registry.register_action(ActionDefinition("test.disabled", "test", "Disabled", _echo))
        result = ExtensionRuntime(registry=registry).execute_action("test.disabled")

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "plugin_not_enabled")
        self.assertEqual(result.error.details["plugin_status"], "disabled")

    def test_runtime_register_action_auto_enables_implicit_plugin(self):
        runtime = ExtensionRuntime()
        runtime.register_action(ActionDefinition("ext.echo", "ext", "Echo", _echo))

        result = runtime.execute_action("ext.echo", {"symbol": "600519"}, {"caller": "agent"})

        self.assertTrue(result.ok)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.data["payload"]["symbol"], "600519")

    def test_invalid_context_and_required_input_fail_before_execution(self):
        invalid_context = self._runtime().execute_action(
            "test.echo",
            context={"budget": {"timeout_seconds": "abc"}},
        )

        task_runner = StubTaskRunner()
        runtime = self._runtime(
            ActionDefinition(
                "test.async_required",
                "test",
                "Async required",
                _echo,
                input_schema={"type": "object", "required": ["symbol"]},
                mode=ActionMode.ASYNC,
            ),
            task_runner=task_runner,
        )
        invalid_input = runtime.execute_action("test.async_required", {}, {"caller": "agent"})

        self.assertEqual(invalid_context.error.code, "invalid_context")
        self.assertEqual(invalid_input.error.code, "invalid_input")
        self.assertEqual(invalid_input.error.details["missing_fields"], ["symbol"])
        self.assertEqual(task_runner.calls, [])

    def test_dry_run_validates_without_invoking_handler(self):
        calls = []
        task_runner = StubTaskRunner()

        def side_effecting(payload, context):
            calls.append(payload)
            return {"called": True}

        action = ActionDefinition(
            "test.confirmed",
            "test",
            "Confirmed",
            side_effecting,
            input_schema={"type": "object", "required": ["symbol"]},
            mode=ActionMode.ASYNC,
            requires_confirmation=True,
        )
        result = self._runtime(action, task_runner=task_runner).execute_action(
            "test.confirmed",
            {"symbol": "600519"},
            {"dry_run": "true"},
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.data, {"status": "validated", "dry_run": True})
        self.assertEqual(calls, [])
        self.assertEqual(task_runner.calls, [])

    def test_dry_run_rejects_invalid_schema_payload(self):
        result = create_builtin_extension_runtime().execute_action(
            "stock_pool.import",
            {"items": "not-a-list"},
            {"dry_run": True},
        )

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "invalid_input")
        self.assertEqual(result.error.details["field_errors"], {"items": "must be an array"})

    def test_async_submission_failure_returns_structured_error(self):
        class FailingTaskRunner:
            def submit(self, **kwargs):
                raise RuntimeError("executor shutdown")

        runtime = self._runtime(
            ActionDefinition("test.async_echo", "test", "Async echo", _echo, mode=ActionMode.ASYNC),
            task_runner=FailingTaskRunner(),
        )
        result = runtime.execute_action("test.async_echo", {"symbol": "600519"})

        self.assertFalse(result.ok)
        self.assertEqual(result.error.code, "task_submission_failed")

    def test_timeout_returns_failure_without_waiting_for_handler(self):
        started = threading.Event()
        completed = threading.Event()
        events = []

        def slow(payload, context):
            started.set()
            events.append("started")
            time.sleep(0.2)
            events.append("done")
            completed.set()
            return {"done": True}

        start_at = time.perf_counter()
        result = self._runtime(ActionDefinition("test.slow", "test", "Slow", slow, timeout_seconds=0.01)).execute_action(
            "test.slow"
        )
        duration = time.perf_counter() - start_at

        self.assertLess(duration, 0.1)
        self.assertEqual(result.error.code, "timeout")
        self.assertTrue(started.is_set())
        self.assertFalse(completed.is_set())
        self.assertTrue(completed.wait(1))
        self.assertEqual(events, ["started", "done"])

    def test_builtin_runtime_registers_core_actions(self):
        runtime = create_builtin_extension_runtime()
        action_ids = {action.action_id for action in runtime.registry.list_actions()}
        self.assertEqual({"dsa.analyze_stock", "notification.send", "stock_pool.import"} - action_ids, set())

        dry_run = runtime.execute_action("notification.send", {"channel": "test"}, {"dry_run": True})
        blocked = runtime.execute_action("notification.send", {"channel": "test"})
        blocked_string = runtime.execute_action(
            "notification.send",
            {"channel": "test"},
            {"requires_confirmation": "false"},
        )
        confirmed = runtime.execute_action("notification.send", {"channel": "test"}, {"requires_confirmation": True})
        stock_pool_missing = runtime.execute_action("stock_pool.import", {}, {"requires_confirmation": True})
        stock_pool_confirmed = runtime.execute_action(
            "stock_pool.import",
            {"items": []},
            {"requires_confirmation": True},
        )

        self.assertTrue(dry_run.ok)
        self.assertEqual(dry_run.data["status"], "validated")
        self.assertEqual(blocked.error.code, "confirmation_required")
        self.assertEqual(blocked_string.error.code, "confirmation_required")
        self.assertTrue(confirmed.ok)
        self.assertEqual(confirmed.degradation["code"], "adapter_not_bound")
        self.assertNotIn("degradation", confirmed.data)
        self.assertEqual(stock_pool_missing.error.code, "invalid_input")
        self.assertTrue(stock_pool_confirmed.ok)
        self.assertEqual(stock_pool_confirmed.degradation["code"], "adapter_not_bound")


if __name__ == "__main__":
    unittest.main()
