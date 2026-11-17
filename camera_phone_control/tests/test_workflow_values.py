"""Value outputs and currency parsing without physical phones or Paddle startup."""

import os
import tempfile
import threading
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock, patch

import numpy as np

from automation.context import Cancelled
from automation.screen.camera_ocr import CameraOCRScreenStateProvider
from automation.screen.state import ScreenStateError
from automation.workflows.models import new_workflow
from automation.workflows.runner import WorkflowRunner, format_input_value
from automation.workflows.validation import validate_workflow
from automation.workflows.values import parse_currency, read_currency
from automation.workflows.service import WorkflowService
from android_control import AndroidControl, Device
from device_lock import DeviceLocks


ROI = {"x": 0.25, "y": 0.5, "width": 0.5, "height": 0.25}


class WorkflowValueTests(unittest.TestCase):
    def test_amount_input_formats_use_exact_decimal_strings(self):
        raw = {"source": "variable", "variable": "amount"}
        implied = {**raw, "amount_format": "implied_cents"}
        whole = {**raw, "amount_format": "whole_number"}
        self.assertEqual(format_input_value(raw, "100.00"), "100.00")
        for value, expected in (
            ("100.00", "10000"),
            ("25.50", "2550"),
            ("25.5", "2550"),
            ("1", "100"),
            ("0.50", "50"),
            ("0.05", "5"),
            ("0.00", "0"),
            ("1234.56", "123456"),
        ):
            with self.subTest(value=value):
                self.assertEqual(format_input_value(implied, value), expected)
        self.assertEqual(format_input_value(whole, "100.00"), "100")
        self.assertEqual(format_input_value(whole, "25.00"), "25")

    def test_amount_input_formats_reject_ambiguous_or_lossy_values(self):
        implied = {
            "source": "variable",
            "variable": "amount",
            "amount_format": "implied_cents",
        }
        for value in ("", "abc", "12.345", "1,234.56", "-10.00", "10..00"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                format_input_value(implied, value)
        with self.assertRaisesRegex(ValueError, "whole amounts only; received 25.50"):
            format_input_value(
                {**implied, "amount_format": "whole_number"}, "25.50"
            )

    def test_non_amount_variables_are_unchanged(self):
        for name, value in (
            ("account_number", "001234"),
            ("bank_name", "Example Bank"),
            ("custom_value", "custom"),
        ):
            with self.subTest(name=name):
                self.assertEqual(
                    format_input_value(
                        {
                            "source": "variable",
                            "variable": name,
                            "amount_format": "implied_cents",
                        },
                        value,
                    ),
                    value,
                )

    def test_hid_and_screen_keyboard_share_amount_formatting(self):
        ctx = SimpleNamespace(
            check_cancelled=Mock(),
            input=Mock(),
            wait=Mock(),
            logger=Mock(),
        )
        runner = WorkflowRunner(ctx)
        base = {
            "type": "input",
            "source": "variable",
            "variable": "amount",
            "amount_format": "implied_cents",
        }
        values = {"amount": "25.50"}
        runner.execute_step(base, values)
        ctx.input.type_text.assert_called_once_with("2550")
        self.assertEqual(values["amount"], "25.50")

        ctx.input.reset_mock()
        keyboard = {
            "kind": "numeric",
            "keys": {
                digit: {"x": index / 10, "y": 0.5}
                for index, digit in enumerate("0123456789")
            },
        }
        runner.execute_step(
            {**base, "input_method": "screen_keyboard", "keyboard_id": "digits"},
            {"amount": "25.50"},
            {"digits": keyboard},
        )
        self.assertEqual(ctx.input.tap.call_count, 4)
        ctx.input.type_text.assert_not_called()

    def test_validation_accepts_missing_format_as_raw_and_scopes_explicit_format(self):
        workflow = new_workflow("Payment")
        workflow["variables"] = [
            {
                "name": "amount",
                "label": "Amount",
                "type": "text",
                "required": True,
                "default": None,
            }
        ]
        step = {
            "id": "amount",
            "type": "input",
            "source": "variable",
            "variable": "amount",
        }
        workflow["steps"] = [step]
        self.assertNotIn("amount_format", validate_workflow(workflow)["steps"][0])
        workflow["steps"] = [{**step, "amount_format": "implied_cents"}]
        self.assertEqual(
            validate_workflow(workflow)["steps"][0]["amount_format"],
            "implied_cents",
        )
        workflow["variables"].append(
            {
                "name": "account_number",
                "label": "Account Number",
                "type": "text",
                "required": True,
                "default": None,
            }
        )
        workflow["steps"] = [
            {**step, "variable": "account_number", "amount_format": "raw"}
        ]
        with self.assertRaisesRegex(ValueError, "only valid for the amount"):
            validate_workflow(workflow)

    def test_currency_variants_are_decimal(self):
        for value in (
            "RM 8,230.50", "RM8,230.50", "RM8230.50", "8,230.50",
            "Available Balance RM 8,230.50", "MYR 8,230.50", "0.00",
        ):
            with self.subTest(value=value):
                self.assertEqual(parse_currency(value), Decimal("0.00" if value == "0.00" else "8230.50"))
                self.assertIsInstance(parse_currency(value), Decimal)

    def test_ambiguous_or_malformed_currency_is_rejected(self):
        for value in (
            "", "RM", "8230", "RM 8,23O.50", "8,23.50", "8.230,50",
            "-12.34", "12.345", "RM 10.00 MYR 20.00", "12.34 garbage",
            "garbage 12.34", "NaN", "Infinity", "12.34 12.34", 12.34,
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_currency(value)

    def provider(self, text="RM 8,230.50"):
        provider = SimpleNamespace(
            capture_snapshot=Mock(return_value=np.zeros((100, 80, 3), np.uint8)),
            _crop_roi=CameraOCRScreenStateProvider._crop_roi,
            visible_texts_from_image=Mock(return_value=[text]),
        )
        return provider

    def test_only_calibrated_roi_goes_to_ocr(self):
        provider = self.provider()
        ctx = SimpleNamespace(check_cancelled=Mock())
        self.assertEqual(read_currency(provider, ROI, ctx), Decimal("8230.50"))
        image = provider.visible_texts_from_image.call_args.args[0]
        self.assertEqual(image.shape, (25, 40, 3))
        self.assertEqual(ctx.check_cancelled.call_count, 3)

    def test_cancellation_and_parse_failure_do_not_produce_outputs(self):
        ctx = SimpleNamespace(check_cancelled=Mock(side_effect=Cancelled()))
        provider = self.provider()
        with self.assertRaises(Cancelled):
            read_currency(provider, ROI, ctx)
        provider.capture_snapshot.assert_not_called()
        ctx.check_cancelled = Mock()
        with self.assertRaises(ScreenStateError):
            read_currency(self.provider("unknown"), ROI, ctx)

    def test_runner_outputs_currency_without_logging_value(self):
        output = Mock()
        ctx = SimpleNamespace(check_cancelled=Mock(), logger=Mock())
        runner = WorkflowRunner(ctx, screen_state_provider=self.provider(), output_callback=output)
        runner.execute_step({"type": "ocr_value", "roi": ROI, "parser": "currency", "output": "payer_balance"})
        output.assert_called_once_with("payer_balance", "8230.50")
        self.assertNotIn("8230", str(ctx.logger.mock_calls))

    def test_concurrent_service_runs_keep_device_outputs_separate(self):
        both_reading = threading.Barrier(2)
        providers = {"phone_A": self.provider(), "phone_B": self.provider("RM 99.00")}
        for device_id, provider in providers.items():
            amount = "RM 8230.50" if device_id == "phone_A" else "RM 99.00"

            def recognize(image, text=amount):
                both_reading.wait(timeout=3)
                return [text]

            provider.visible_texts_from_image.side_effect = recognize

        flow = new_workflow("Balance")
        flow["steps"] = [{"id": "read", "type": "ocr_value", "roi": ROI,
                          "parser": "currency", "output": "payer_balance"}]
        devices = {key: Device(key, 1080, 1920, 0) for key in providers}
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ, PHONE_INPUT_BACKEND="appium", PHONE_SCREEN_BACKEND="appium"
        ), patch("automation.runner.select_device", side_effect=lambda *args, device_id=None: devices[device_id]):
            service = WorkflowService(
                Mock(spec=AndroidControl), DeviceLocks(directory),
                screen_resolver=lambda device_id: (None, providers[device_id]),
            )
            completions = []

            def completed(snapshot):
                self.assertFalse(service.has_active_run(snapshot["device_id"]))
                completions.append(snapshot)

            service.add_completion_listener(completed)
            a = service.start(flow, {}, device_id="phone_A")
            b = service.start(flow, {}, device_id="phone_B")
            results = [service.wait(run["run_id"], 5) for run in (a, b)]
            self.assertEqual([result["status"] for result in results], ["passed", "passed"])
            self.assertEqual(results[0]["outputs"], {"payer_balance": "8230.50"})
            self.assertEqual(results[1]["outputs"], {"payer_balance": "99.00"})
            results[0]["outputs"]["payer_balance"] = "0.00"
            self.assertEqual(service.snapshot(a["run_id"])["outputs"]["payer_balance"], "8230.50")
            self.assertEqual(
                {
                    item["device_id"]: item["outputs"]["payer_balance"]
                    for item in completions
                },
                {"phone_A": "8230.50", "phone_B": "99.00"},
            )

    def test_validation_requires_roi_parser_and_output(self):
        workflow = new_workflow("Balance")
        step = {"id": "read", "type": "ocr_value", "roi": ROI, "parser": "currency", "output": "payer_balance"}
        workflow["steps"] = [step]
        self.assertEqual(validate_workflow(workflow)["steps"][0]["roi"], ROI)
        for field in ("roi", "parser", "output"):
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_workflow({**workflow, "steps": [{key: value for key, value in step.items() if key != field}]})

    def test_recorder_read_value_can_be_edited_saved_moved_and_deleted(self):
        from automation.workflows.recorder import Recorder
        from automation.workflows.storage import WorkflowStorage
        import tempfile

        recorder = Recorder()
        recorder.start("test-owner", "Balance", execute=False)
        recorder.stop("test-owner")
        first = recorder.review("test-owner", "insert", {"step": {
            "type": "ocr_value", "parser": "currency", "output": "payer_balance", "roi": ROI,
        }})
        sid = first["workflow"]["steps"][0]["id"]
        changed = recorder.review("test-owner", "fields", {"step_id": sid, "fields": {"output": "balance"}})
        self.assertEqual(changed["workflow"]["steps"][0]["output"], "balance")
        with tempfile.TemporaryDirectory() as directory:
            store = WorkflowStorage(directory)
            saved = store.save(changed["workflow"], create=True)
            self.assertEqual(store.read(saved["id"])["steps"][0]["roi"], ROI)
        recorder.review("test-owner", "insert", {"step": {"type": "capture_screen", "output_key": "receipt_path"}})
        moved = recorder.review("test-owner", "move", {"step_id": sid, "index": 1})
        self.assertEqual(moved["workflow"]["steps"][1]["id"], sid)
        deleted = recorder.review("test-owner", "delete", {"step_id": sid})
        self.assertEqual(deleted["workflow"]["steps"][0]["type"], "capture_screen")


if __name__ == "__main__":
    unittest.main()
