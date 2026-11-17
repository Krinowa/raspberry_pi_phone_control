import io
import logging
import threading
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch

from automation.screen.camera_ocr import (
    CameraOCRScreenStateProvider as PaddleOCRProvider,
)
from automation.screen.camera_ocr_tesseract import (
    CameraOCRScreenStateProvider as TesseractOCRProvider,
)
from automation.screen.state import (
    ScreenStateError,
    VERIFY_TEXT_FAILURE,
    account_name_ocr_candidates,
    text_verification_matches,
    verify_text_on_screen,
)
from automation.workflows.models import new_workflow
from automation.workflows.recorder import Recorder
from automation.workflows.runner import WorkflowRunner
from automation.workflows.validation import validate_workflow


def workflow(step, variables=None):
    result = new_workflow("Verify")
    result["id"] = "verify_flow"
    result["variables"] = variables or []
    result["steps"] = [step]
    return result


def variable(name="account_name"):
    return {
        "name": name,
        "label": name.replace("_", " ").title(),
        "type": "text",
        "required": True,
        "default": "STAGING TEST",
    }


class Context:
    def __init__(self):
        self.cancel_event = threading.Event()
        self.input = Mock()
        self.state = SimpleNamespace()
        self.output = io.StringIO()
        self.logger = logging.Logger("verify-text-test", logging.INFO)
        self.logger.handlers = [logging.StreamHandler(self.output)]

    def check_cancelled(self):
        return None

    def wait(self, seconds):
        return None


class Provider:
    def __init__(self, texts):
        self.texts = texts

    def visible_texts(self):
        return list(self.texts)

    def visible_texts_in_roi(self, roi):
        return list(self.texts)


class CandidateProvider(Provider):
    def __init__(self, candidates, roi_candidates=None):
        self.candidates = candidates
        self.roi_candidates = candidates if roi_candidates is None else roi_candidates

    def visible_text_candidates(self):
        return list(self.candidates)

    def visible_text_candidates_in_roi(self, roi):
        return list(self.roi_candidates)

    def visible_texts(self):
        return [candidate["text"] for candidate in self.candidates]

    def visible_texts_in_roi(self, roi):
        return [candidate["text"] for candidate in self.roi_candidates]


class VerifyTextTests(unittest.TestCase):
    def fixed(self, **overrides):
        return {
            "id": "verify",
            "type": "verify_text",
            "source": "fixed",
            "text": "Staging Test",
            "match_mode": "exact",
            "timeout_seconds": 0.5,
            **overrides,
        }

    def test_match_modes_and_normalization(self):
        self.assertTrue(text_verification_matches(["STAGING   TEST"], "staging test", "exact"))
        self.assertFalse(text_verification_matches(["Staging Test Ltd"], "staging test", "exact"))
        self.assertTrue(text_verification_matches(["Recipient: Staging Test"], "staging test", "contains"))
        self.assertFalse(text_verification_matches(["Recipient: Other"], "staging test", "contains"))
        self.assertTrue(text_verification_matches(["******7890"], "1234567890", "last_n_digits", 4))
        self.assertFalse(text_verification_matches(["******1234"], "1234567890", "last_n_digits", 4))
        self.assertFalse(text_verification_matches([], "", "contains"))

    def test_automatic_bank_account_and_amount_matching(self):
        self.assertTrue(
            text_verification_matches(
                ["Hong Leong Bank Berhad"], "Hong Leong Bank", "contains"
            )
        )
        self.assertTrue(
            text_verification_matches(
                ["123-456 7890"], "1234567890", "account_number"
            )
        )
        self.assertFalse(
            text_verification_matches(
                ["923-456 7890"], "1234567890", "account_number"
            )
        )
        self.assertTrue(
            text_verification_matches(
                ["******7890"], "1234567890", "account_number"
            )
        )
        self.assertTrue(
            text_verification_matches(["7890"], "1234567890", "account_number")
        )
        self.assertFalse(
            text_verification_matches(["***890"], "1234567890", "account_number")
        )
        for displayed in ("1.02", "RM 1.02", "RM1.02", "MYR 1.02", "MYR1.02"):
            with self.subTest(displayed=displayed):
                self.assertTrue(
                    text_verification_matches([displayed], "1.02", "amount")
                )
        self.assertTrue(
            text_verification_matches(["RM 1,000.00"], "1000.00", "amount")
        )
        self.assertFalse(text_verification_matches(["RM 1.20"], "1.02", "amount"))
        self.assertFalse(text_verification_matches(["RM 11.02"], "1.02", "amount"))

    def test_account_name_full_joint_and_masked_matching(self):
        expected = "LEE YONG WEI"
        for displayed in (
            "LEE YONG WEI",
            "lee yong wei",
            "LEE   YONG  WEI",
            "LEE, YONG WEI",
            "LEE, Y** W*",
            "LEE YONG WEI O/B TAN ABC",
            "TAN ABC O/B LEE YONG WEI",
        ):
            with self.subTest(displayed=displayed):
                self.assertTrue(
                    text_verification_matches([displayed], expected, "account_name")
                )

        for displayed in (
            "LEE KOK MING",
            "LEE, Y** W*",
            "W* Y** LEE",
            "L** ***",
            "*** Y***",
            "TAN ABC O/B LIM KOK MING",
        ):
            checked_expected = "LEE KOK MING" if displayed == "LEE, Y** W*" else expected
            with self.subTest(displayed=displayed, expected=checked_expected):
                self.assertFalse(
                    text_verification_matches(
                        [displayed], checked_expected, "account_name"
                    )
                )

    def test_account_name_reconstructs_adjacent_wrapped_joint_holder(self):
        candidates = [
            {"text": "CHONG FK O/B KOH WEI", "box": [20, 100, 300, 120]},
            {"text": "ZHE", "box": [20, 124, 70, 144]},
        ]

        verify_text_on_screen(
            CandidateProvider(candidates),
            Context(),
            "KOH WEI ZHE",
            0.5,
            match_mode="account_name",
        )

    def test_account_name_multiline_grouping_is_spatial_and_conservative(self):
        distant = [
            {"text": "KOH WEI", "box": [20, 100, 150, 120]},
            {"text": "ZHE", "box": [20, 240, 70, 260]},
        ]
        bank_line = [
            {"text": "CHONG FK O/B KOH WEI", "box": [20, 100, 300, 120]},
            {"text": "PUBLIC BANK/PUBLIC", "box": [20, 124, 250, 144]},
        ]
        scattered = [
            {"text": "KOH", "box": [20, 100, 60, 120]},
            {"text": "WEI", "box": [20, 200, 60, 220]},
            {"text": "ZHE", "box": [20, 300, 60, 320]},
        ]
        wrong_holder = [
            {"text": "CHONG FK O/B TAN AH KOW", "box": [20, 100, 300, 120]},
            {"text": "ZHE", "box": [20, 124, 70, 144]},
        ]

        self.assertNotIn("KOH WEI ZHE", account_name_ocr_candidates(distant))
        self.assertNotIn(
            "CHONG FK O/B KOH WEI PUBLIC BANK/PUBLIC",
            account_name_ocr_candidates(bank_line),
        )
        self.assertNotIn("KOH WEI ZHE", account_name_ocr_candidates(scattered))
        self.assertFalse(
            text_verification_matches(
                account_name_ocr_candidates(wrong_holder),
                "KOH WEI ZHE",
                "account_name",
            )
        )

    def test_account_name_multiline_preserves_spatial_reading_order(self):
        candidates = [
            {"text": "ZHE", "box": [20, 124, 70, 144]},
            {"text": "CHONG FK O/B KOH WEI", "box": [20, 100, 300, 120]},
        ]

        reconstructed = account_name_ocr_candidates(candidates)

        self.assertIn("CHONG FK O/B KOH WEI ZHE", reconstructed)
        self.assertNotIn("ZHE CHONG FK O/B KOH WEI", reconstructed)

    def test_account_name_roi_joins_only_lines_returned_inside_roi(self):
        inside = [
            {"text": "CHONG FK O/B KOH WEI", "box": [20, 20, 300, 40]},
            {"text": "ZHE", "box": [20, 44, 70, 64]},
        ]
        outside = {"text": "UNRELATED", "box": [20, 300, 150, 320]}
        provider = CandidateProvider([*inside, outside], roi_candidates=inside)

        verify_text_on_screen(
            provider,
            Context(),
            "KOH WEI ZHE",
            0.5,
            match_mode="account_name",
            roi={"x": 0.1, "y": 0.1, "width": 0.8, "height": 0.2},
        )

    def test_paddle_and_tesseract_candidates_retain_line_boxes(self):
        paddle = PaddleOCRProvider._candidates_from_results(
            [
                {
                    "res": {
                        "rec_texts": ["CHONG FK O/B KOH WEI", "ZHE"],
                        "rec_scores": [0.99, 0.98],
                        "rec_boxes": [[20, 100, 300, 120], [20, 124, 70, 144]],
                    }
                }
            ]
        )
        tesseract = TesseractOCRProvider._candidates_from_tsv(
            "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
            "5\t1\t1\t1\t1\t1\t20\t100\t70\t20\t99\tCHONG FK O/B KOH WEI\n"
            "5\t1\t1\t1\t2\t1\t20\t124\t50\t20\t98\tZHE\n"
        )

        for candidates in (paddle, tesseract):
            with self.subTest(engine=candidates):
                self.assertTrue(all(candidate.get("box") for candidate in candidates))
                self.assertTrue(
                    text_verification_matches(
                        account_name_ocr_candidates(candidates),
                        "KOH WEI ZHE",
                        "account_name",
                    )
                )

    def test_tesseract_diagnostics_do_not_log_recognized_names(self):
        provider = TesseractOCRProvider.__new__(TesseractOCRProvider)
        provider.debug_logging = False
        raw = "PRIVATE RECIPIENT\nSECOND PRIVATE HOLDER\n"

        with self.assertLogs("camera_ocr", level="DEBUG") as captured:
            provider._log_result(
                raw,
                mode="grayscale",
                scale=1.0,
                psm=11,
                output_format=None,
                duration=0.01,
            )

        output = "\n".join(captured.output)
        self.assertIn("lines=2", output)
        self.assertNotIn("PRIVATE RECIPIENT", output)
        self.assertNotIn("SECOND PRIVATE HOLDER", output)

    def test_fixed_and_variable_exact_pass(self):
        for step, values in (
            (self.fixed(), None),
            ({
                "id": "verify",
                "type": "verify_text",
                "source": "variable",
                "variable": "account_name",
                "match_mode": "exact",
                "timeout_seconds": 0.5,
            }, {"account_name": "STAGING TEST"}),
        ):
            ctx = Context()
            flow = workflow(step, [variable()] if values else None)
            WorkflowRunner(ctx, screen_state_provider=Provider(["Staging   Test"])).run(flow, values)
            ctx.input.tap.assert_not_called()

    def test_mismatch_uses_safe_error_and_stops_later_tap(self):
        ctx = Context()
        flow = workflow(
            {
                "id": "verify",
                "type": "verify_text",
                "source": "variable",
                "variable": "account_name",
                "match_mode": "account_name",
                "timeout_seconds": 0.5,
            },
            [variable()],
        )
        flow["steps"].append({"id": "confirm", "type": "tap", "x": 0.5, "y": 0.5})
        with patch(
            "automation.workflows.runner.verify_text_on_screen",
            side_effect=ValueError(VERIFY_TEXT_FAILURE),
        ):
            with self.assertRaisesRegex(ValueError, "Payment confirmation was not sent"):
                WorkflowRunner(ctx, screen_state_provider=Provider(["Other"])).run(
                    flow, {"account_name": "PRIVATE RECIPIENT"}
                )
        ctx.input.tap.assert_not_called()
        self.assertNotIn("PRIVATE RECIPIENT", ctx.output.getvalue())
        self.assertNotIn("Other", ctx.output.getvalue())

    def test_failed_multiline_account_name_stops_tap_without_name_logs(self):
        ctx = Context()
        flow = workflow(
            {
                "id": "verify",
                "type": "verify_text",
                "source": "variable",
                "variable": "account_name",
                "match_mode": "account_name",
                "timeout_seconds": 0.5,
            },
            [variable()],
        )
        flow["steps"].append({"id": "confirm", "type": "tap", "x": 0.5, "y": 0.5})
        provider = CandidateProvider(
            [
                {"text": "CHONG FK O/B TAN AH KOW", "box": [20, 100, 300, 120]},
                {"text": "ZHE", "box": [20, 124, 70, 144]},
            ]
        )

        with self.assertRaisesRegex(
            ScreenStateError, "Payment confirmation was not sent"
        ):
            WorkflowRunner(ctx, screen_state_provider=provider).run(
                flow, {"account_name": "KOH WEI ZHE"}
            )

        ctx.input.tap.assert_not_called()
        output = ctx.output.getvalue()
        self.assertNotIn("KOH WEI ZHE", output)
        self.assertNotIn("CHONG FK", output)
        self.assertNotIn("TAN AH KOW", output)

    def test_variable_value_is_not_logged(self):
        ctx = Context()
        step = {
            "id": "verify",
            "type": "verify_text",
            "source": "variable",
            "variable": "account_name",
            "match_mode": "account_name",
            "timeout_seconds": 0.5,
        }
        WorkflowRunner(ctx, screen_state_provider=Provider(["PRIVATE RECIPIENT"])).run(
            workflow(step, [variable()]), {"account_name": "PRIVATE RECIPIENT"}
        )
        self.assertNotIn("PRIVATE RECIPIENT", ctx.output.getvalue())
        self.assertIn("field=account_name", ctx.output.getvalue())

    def test_fixed_mismatch_raises_required_safe_error(self):
        ctx = Context()
        times = iter((0.0, 1.0))
        with self.assertRaisesRegex(
            ScreenStateError, "Payment confirmation was not sent"
        ):
            verify_text_on_screen(
                Provider(["Other"]),
                ctx,
                "Staging Test",
                0.5,
                match_mode="exact",
                clock=lambda: next(times),
            )

    def test_validation_rejects_invalid_configurations(self):
        invalid = [
            self.fixed(source="unknown"),
            self.fixed(match_mode="fuzzy"),
            self.fixed(match_mode="last_n_digits", last_n=0),
            self.fixed(last_n=4),
            self.fixed(variable="account_name"),
            self.fixed(roi={"x": 0.9, "y": 0.2, "width": 0.2, "height": 0.2}),
            {
                "id": "verify",
                "type": "verify_text",
                "source": "variable",
                "variable": "missing",
                "match_mode": "exact",
                "timeout_seconds": 5,
            },
            {
                "id": "verify",
                "type": "verify_text",
                "source": "variable",
                "variable": "account_name",
                "text": "conflict",
                "match_mode": "exact",
                "timeout_seconds": 5,
            },
        ]
        for step in invalid:
            with self.subTest(step=step):
                with self.assertRaises(ValueError):
                    validate_workflow(workflow(deepcopy(step), [variable()]))

    def test_recorder_provisions_and_retains_all_standard_verify_variables(self):
        recorder = Recorder()
        recorder.start("owner", "Verify")
        recorder.stop("owner")
        mappings = {
            "account_name": "account_name",
            "account_number": "account_number",
            "bank_name": "contains",
            "amount": "amount",
        }
        result = None
        for index, (name, match_mode) in enumerate(mappings.items()):
            result = recorder.review(
                "owner",
                "insert",
                {
                    "index": index,
                    "step": {
                        "type": "verify_text",
                        "source": "variable",
                        "variable": name,
                        "match_mode": match_mode,
                        "timeout_seconds": 5,
                    },
                },
            )
        self.assertEqual(
            [item["name"] for item in result["workflow"]["variables"]],
            list(mappings),
        )
        self.assertEqual(
            [step["variable"] for step in result["workflow"]["steps"]],
            list(mappings),
        )

    def test_recorder_can_edit_verification_source_without_conflicting_fields(self):
        recorder = Recorder()
        recorder.start("owner", "Verify")
        recorder.workflow["variables"] = [variable()]
        recorder.workflow["steps"] = [
            {
                "id": "verify",
                "type": "verify_text",
                "source": "variable",
                "variable": "account_name",
                "match_mode": "last_n_digits",
                "last_n": 4,
                "timeout_seconds": 5,
            }
        ]
        recorder.stop("owner")
        result = recorder.review(
            "owner",
            "fields",
            {
                "step_id": "verify",
                "fields": {
                    "source": "fixed",
                    "text": "Recipient",
                    "match_mode": "contains",
                    "timeout_seconds": 5,
                },
            },
        )
        step = result["workflow"]["steps"][0]
        self.assertNotIn("variable", step)
        self.assertNotIn("last_n", step)
        self.assertEqual(step["text"], "Recipient")

    def test_recorder_round_trip_changes_standard_verify_field(self):
        recorder = Recorder()
        recorder.start("owner", "Verify")
        recorder.stop("owner")
        inserted = recorder.review(
            "owner",
            "insert",
            {
                "index": 0,
                "step": {
                    "type": "verify_text",
                    "source": "variable",
                    "variable": "account_name",
                    "match_mode": "exact",
                    "timeout_seconds": 5,
                },
            },
        )
        step_id = inserted["workflow"]["steps"][0]["id"]
        updated = recorder.review(
            "owner",
            "fields",
            {
                "step_id": step_id,
                "fields": {
                    "source": "variable",
                    "variable": "amount",
                    "match_mode": "amount",
                    "timeout_seconds": 5,
                },
            },
        )
        self.assertEqual(updated["workflow"]["steps"][0]["variable"], "amount")
        self.assertEqual(
            [item["name"] for item in updated["workflow"]["variables"]], ["amount"]
        )

    def test_wait_for_text_remains_supported(self):
        validated = validate_workflow(
            workflow({"id": "wait", "type": "wait_for_text", "text": "Ready", "timeout_seconds": 1})
        )
        self.assertEqual(validated["steps"][0]["type"], "wait_for_text")


if __name__ == "__main__":
    unittest.main()
