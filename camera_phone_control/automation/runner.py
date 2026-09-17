"""Run predefined Android scripts. Usage: python -m automation.runner --list"""

import argparse
import logging
import signal
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

from android_control import AndroidControl, DeviceError
from device_lock import DeviceBusy, DeviceLocks
from .context import AutomationContext, Cancelled
from .scripts import SCRIPTS, available_scripts

LOG = logging.getLogger("automation")


def now():
    return datetime.now(timezone.utc).isoformat()


@dataclass
class RunState:
    script_name: str
    device_serial: str | None = None
    status: str = "running"
    start_time: str | None = None
    end_time: str | None = None
    current_step: str | None = None
    message: str = "Starting script."
    takeover_requested: bool = False

    @property
    def exit_code(self):
        return (
            0
            if self.status == "completed"
            else 130 if self.status == "cancelled" else 1
        )


class Runner:
    """One run at a time per instance; separate instances may target other devices."""

    def __init__(self, controller=None, device_locks=None):
        self.controller = controller or AndroidControl()
        self.device_locks = device_locks or DeviceLocks()
        self.state = None
        self._run_lock = threading.Lock()

    def run(self, script_name, serial=None, *, cancel_event=None):
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("This runner is already running a script.")
        try:
            return self._run(script_name, serial, cancel_event or threading.Event())
        finally:
            self._run_lock.release()

    def _run(self, script_name, serial, cancel_event):
        script = SCRIPTS.get(script_name)
        self.state = state = RunState(
            script_name if script else "unknown", start_time=now()
        )
        stage = "selection"
        acquired = False
        try:
            if script is None:
                state.status = "failed"
                state.message = "Unknown script. Use --list to see available scripts."
                return state

            if cancel_event.is_set():
                raise Cancelled()

            device = self.controller.device(serial=serial, authorized_only=True)
            state.device_serial = device.serial

            if cancel_event.is_set():
                raise Cancelled()

            stage = "locking"
            with self.device_locks.hold(device.serial, "automation") as ownership:
                acquired = True
                LOG.info(
                    "Automation ownership acquired. Lock acquired on %s", device.serial
                )
                LOG.info("Script started: %s on %s", script_name, device.serial)
                stage = "script"

                context = AutomationContext(
                    device,
                    self.controller,
                    cancel_event,
                    LOG,
                    state,
                    ownership.takeover_requested,
                )

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
            state.message = f"Device busy ({exc.owner}). No script input sent."
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
        except (Exception, SystemExit):
            state.status = "failed"
            state.message = (
                "Cannot acquire the local device lock. Check lock directory permissions."
                if stage == "locking"
                else "Script failed. Check the script implementation at the reported step; no retry attempted."
            )
        finally:
            if acquired:
                LOG.info(
                    "Automation ownership released. Lock released on %s",
                    state.device_serial,
                )
            state.end_time = now()
            # No exception strings, arguments, input values, or script locals here.
            log = LOG.error if state.status == "failed" else LOG.info
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
        return run(args.script, args.serial, cancel_event=cancel_event).exit_code
    finally:
        signal.signal(signal.SIGINT, previous)


if __name__ == "__main__":
    raise SystemExit(main())
