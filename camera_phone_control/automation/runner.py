"""Run predefined Android scripts. Usage: python -m automation.runner --list"""

import argparse
import logging
import signal
import threading
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime, timezone

from android_control import AndroidControl, DeviceError
from device_lock import DeviceBusy, DeviceLocks
from hardware.pi_api_client import PiApiError
from .context import AutomationContext, Cancelled
from .backends import select_device, backend_names
from .input.base import UnsupportedOperation
from .screen.state import ScreenStateError
from .screen.stability import ScreenStabilityError
from .workflows.timing import measure_timing
from .scripts import SCRIPTS, available_scripts

LOG = logging.getLogger("automation")


def now():
    return datetime.now(timezone.utc).isoformat()


@dataclass
class RunState:
    script_name: str
    device_serial: str | None = None
    device_id: str | None = None
    status: str = "running"
    start_time: str | None = None
    end_time: str | None = None
    current_step: str | None = None
    message: str = "Starting script."
    takeover_requested: bool = False
    step_index: int = 0
    step_id: str | None = None
    step_count: int = 0
    checkpoint_message: str | None = None
    error_code: str | None = None
    error_params: dict = field(default_factory=dict)

    @property
    def exit_code(self):
        return (
            0
            if self.status == "completed"
            else 130 if self.status == "cancelled" else 1
        )


class Runner:
    """One run at a time per instance; separate instances may target other devices."""

    def __init__(self, controller=None, device_locks=None, logger=None):
        self.controller = controller or AndroidControl()
        self.device_locks = device_locks or DeviceLocks()
        self.logger = logger or LOG
        self.state = None
        self._run_lock = threading.Lock()

    def run(self, script_name, serial=None, *, cancel_event=None, device_id=None):
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("This runner is already running a script.")
        try:
            return self._run(
                script_name,
                serial,
                cancel_event or threading.Event(),
                device_id=device_id,
            )
        finally:
            self._run_lock.release()

    def run_task(
        self,
        name,
        task,
        serial=None,
        *,
        cancel_event=None,
        resume_event=None,
        device_id=None,
    ):
        """Use the same device ownership lifecycle for data-driven workflows."""
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("This runner is already running.")
        try:
            return self._run(
                name,
                serial,
                cancel_event or threading.Event(),
                task,
                resume_event,
                device_id=device_id,
            )
        finally:
            self._run_lock.release()

    def _run(
        self,
        script_name,
        serial,
        cancel_event,
        task=None,
        resume_event=None,
        *,
        device_id=None,
    ):
        backend_names()  # Fail with an actionable configuration error before discovery.
        script = task or SCRIPTS.get(script_name)
        self.state = state = RunState(
            script_name if script else "unknown", start_time=now()
        )
        stage = "selection"
        acquired = False
        try:
            if script is None:
                state.status = "failed"
                state.message = "Unknown script. Use --list to see available scripts."
                state.error_code = "workflow_not_found"
                return state

            if cancel_event.is_set():
                raise Cancelled()

            device = select_device(self.controller, serial, device_id=device_id)
            state.device_serial = device.serial
            state.device_id = getattr(device, "device_id", None)

            if cancel_event.is_set():
                raise Cancelled()

            stage = "locking"
            with ExitStack() as stack:
                ownership = stack.enter_context(
                    self.device_locks.hold(device.serial, "automation")
                )
                acquired = True
                self.logger.info(
                    "Automation ownership acquired. Lock acquired on %s", device.serial
                )
                self.logger.info("Script started: %s on %s", script_name, device.serial)
                stage = "script"

                context = AutomationContext(
                    device,
                    self.controller,
                    cancel_event,
                    self.logger,
                    state,
                    lambda: ownership.takeover_requested(),
                )

                def manual_checkpoint(message):
                    nonlocal ownership
                    if resume_event is None:
                        raise RuntimeError(
                            "Manual checkpoints require a resumable runner."
                        )
                    context.check_cancelled()
                    resume_event.clear()
                    stack.close()
                    state.checkpoint_message = message
                    state.status = "waiting_manual"
                    self.logger.info(
                        "Manual checkpoint entered; device ownership released."
                    )
                    with measure_timing("manual_wait"):
                        while not resume_event.wait(0.05):
                            context.check_cancelled()
                    context.check_cancelled()
                    ownership = stack.enter_context(
                        self.device_locks.hold(device.serial, "automation")
                    )
                    context._check_device()
                    state.checkpoint_message = None
                    state.status = "running"
                    self.logger.info(
                        "Manual checkpoint resumed; device ownership acquired."
                    )

                context.manual_handler = manual_checkpoint
                context.check_cancelled()
                script(context)
                context.check_cancelled()
                state.status = "completed"
                state.message = "Script completed."
        except Cancelled as exc:
            state.status = "cancelled"
            state.message = (
                "Script cancelled due to takeover."
                if exc.takeover
                else "Script cancelled. No further steps will run."
            )
        except KeyboardInterrupt:
            state.status = "cancelled"
            state.message = "Script cancelled. No further steps will run."
        except DeviceBusy as exc:
            state.status = "failed"
            state.message = f"Device busy ({exc.owner}). No further input sent."
            state.error_code = "device_busy"
            state.error_params = {"owner": exc.owner}
        except PiApiError as exc:
            state.status = "failed"
            # PiApiClient messages exclude payloads and describe uncertain input.
            state.message = str(exc)
            state.error_code = getattr(exc, "error_code", None)
            state.error_params = getattr(exc, "error_params", {})
        except DeviceError as exc:
            state.status = "failed"
            # Discovery happens before scripts receive any input. Runtime exceptions
            # may contain typed values, so never format them or emit a traceback.
            state.message = (
                str(exc)
                if stage == "selection"
                else (
                    "Android command or health check failed. Check USB/authorization and "
                    "phone geometry. An input may have occurred; it will not be retried."
                )
            )
            state.error_code = "device_command_failed"
        except (UnsupportedOperation, ScreenStabilityError, ScreenStateError) as exc:
            state.status = "failed"
            state.message = str(exc)
            state.error_code = "workflow_failed"
        except (Exception, SystemExit):
            state.status = "failed"
            state.message = (
                "Cannot acquire the local device lock. Check lock directory permissions."
                if stage == "locking"
                else "Script failed. Check the script implementation at the reported step; no retry attempted."
            )
            state.error_code = "workflow_failed"
        finally:
            if acquired:
                self.logger.info(
                    "Automation ownership released. Lock released on %s",
                    state.device_serial,
                )
            state.end_time = now()
            # No exception strings, arguments, input values, or script locals here.
            log = self.logger.error if state.status == "failed" else self.logger.info
            log(
                "Script %s: %s; step=%s. %s",
                state.status,
                state.script_name,
                state.current_step or "setup",
                state.message,
            )
        return state


def run(script_name, serial=None, **kwargs):
    """Trigger-independent entry point, returning a RunState with an exit_code."""
    return Runner().run(script_name, serial, **kwargs)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("script", nargs="?")
    parser.add_argument("--serial", help="Select an authorized physical USB phone")
    parser.add_argument(
        "--device-id",
        help="Select a configured logical phone (defaults to phone_A for Pi input)",
    )
    parser.add_argument("--list", action="store_true", help="List predefined scripts")
    args = parser.parse_args(argv)

    if args.list:
        print("\n".join(available_scripts()))
        return 0
    if not args.script:
        parser.error("a script name is required (or use --list)")

    logging.basicConfig(level=logging.INFO, format="[%(name)s] %(message)s")
    cancel_event = threading.Event()
    previous = signal.signal(signal.SIGINT, lambda signum, frame: cancel_event.set())

    try:
        options = {"cancel_event": cancel_event}
        if args.device_id is not None:
            options["device_id"] = args.device_id
        return run(args.script, args.serial, **options).exit_code
    finally:
        signal.signal(signal.SIGINT, previous)


if __name__ == "__main__":
    raise SystemExit(main())
