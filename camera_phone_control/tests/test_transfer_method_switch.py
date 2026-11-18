"""Regression coverage for API-driven transfer-method workflow branching."""

import unittest
import threading
import tempfile
from types import SimpleNamespace
from unittest.mock import Mock, call

from automation.integrations.withdraw_v2.normalization import normalize_locked_withdrawal
from automation.transfer_methods import TRANSFER_METHODS, transfer_method_options
from automation.workflows.recorder import Recorder
from automation.workflows.runner import WorkflowRunner
from automation.workflows.service import WorkflowService
from automation.workflows.storage import WorkflowStorage
from automation.workflows.validation import resolve_variables, validate_workflow
from automation.workflows.validation import require_transfer_method_branch
from automation.screen.state import text_verification_matches


def variable(name):
    return {
        "name": name,
        "label": name.replace("_", " ").title(),
        "type": "text",
        "required": True,
        "default": None,
    }


def method_branch(method, identifier):
    match_mode = TRANSFER_METHODS[method]["verification_modes"][0]
    return [
        {
            "id": f"input_{method}",
            "type": "input",
            "source": "variable",
            "variable": identifier,
        },
        {
            "id": f"verify_{method}",
            "type": "verify_text",
            "source": "variable",
            "variable": identifier,
            "match_mode": match_mode,
            "timeout_seconds": 2,
        },
    ]


def switching_workflow():
    return {
        "id": "flow_switch",
        "name": "Transfer switch",
        "purpose": "payment",
        "version": 1,
        "variables": [
            variable("account_number"),
            variable("fps_phone_number"),
            variable("fps_email"),
            variable("fps_id"),
        ],
        "keyboards": {},
        "financial": False,
        "steps": [
            {"id": "before", "type": "wait", "seconds": 0},
            {
                "id": "method",
                "type": "switch_transfer_method",
                "branches": {
                    method: method_branch(method, definition["workflow_variable"])
                    for method, definition in TRANSFER_METHODS.items()
                },
            },
            {"id": "after", "type": "wait", "seconds": 0},
        ],
    }


def withdrawal(method, **changes):
    fps = method in {"phone_no", "email", "fps_id"}
    raw = {
        "id": 1,
        "order_id": "ORDER-1",
        "tx_id": "TX-1",
        "final_amount": "10.00",
        "transfer_method": method,
        "type": "kuaizhuan" if fps else "bank",
        "account_name": "Recipient",
        "holder_name": "收款人",
        "bank_name": "Bank",
        "account_number": "001234",
        "mobile_no": {
            "phone_no": "0123-4567",
            "email": "recipient@example.hk",
            "fps_id": "0123456",
        }.get(method, "0123456789"),
    }
    raw.update(changes)
    return raw


class TransferMethodSwitchTests(unittest.TestCase):
    def test_registry_is_single_safe_options_source(self):
        self.assertEqual(
            set(TRANSFER_METHODS), {"bank_account", "phone_no", "email", "fps_id"}
        )
        self.assertEqual(
            {item["value"] for item in transfer_method_options()},
            set(TRANSFER_METHODS),
        )
        self.assertEqual(
            TRANSFER_METHODS["phone_no"]["identifier_field"], "mobile_no"
        )

    def test_workflow_serialization_accepts_explicit_safe_branches(self):
        workflow = switching_workflow()
        validated = validate_workflow(workflow)
        self.assertEqual(validated, workflow)

    def test_empty_unknown_and_wrong_identifier_branches_fail_closed(self):
        for mutate, message in (
            (
                lambda step: step["branches"].__setitem__("phone_no", []),
                "must not be empty",
            ),
            (
                lambda step: step["branches"].__setitem__("future_id", []),
                "unsupported branch",
            ),
            (
                lambda step: step["branches"]["phone_no"].pop(0),
                "must input fps_phone_number",
            ),
        ):
            workflow = switching_workflow()
            mutate(workflow["steps"][1])
            with self.assertRaisesRegex(ValueError, message):
                validate_workflow(workflow)

        workflow = switching_workflow()
        workflow["steps"][1]["branches"].pop("phone_no")
        self.assertEqual(validate_workflow(workflow), workflow)

        workflow = switching_workflow()
        workflow["steps"].insert(
            0,
            {
                "id": "unsafe_shared_identifier",
                "type": "input",
                "source": "variable",
                "variable": "fps_phone_number",
            },
        )
        with self.assertRaisesRegex(ValueError, "must only be used"):
            validate_workflow(workflow)

    def test_runner_selects_exact_branch_and_shared_steps_remain_ordered(self):
        context = SimpleNamespace(
            input=Mock(),
            logger=Mock(),
            state=SimpleNamespace(current_step=None),
            check_cancelled=Mock(),
            wait=Mock(),
        )
        runner = WorkflowRunner(
            context, execution_context={"transfer_method": "phone_no"}
        )
        steps = [
            {"type": "key", "key": "HOME"},
            {
                "type": "switch_transfer_method",
                "branches": {
                    "bank_account": [{"type": "key", "key": "LEFT"}],
                    "phone_no": [{"type": "key", "key": "RIGHT"}],
                },
            },
            {"type": "key", "key": "BACK"},
        ]
        for step in steps:
            runner.execute_step(step, {}, {})
        self.assertEqual(
            context.input.key.call_args_list,
            [call("HOME"), call("RIGHT"), call("BACK")],
        )

    def test_all_four_methods_execute_only_their_mocked_branch(self):
        samples = {
            "bank_account": "001234",
            "phone_no": "0123-4567",
            "email": "recipient@example.hk",
            "fps_id": "0123456",
        }
        switch = switching_workflow()["steps"][1]
        for method, sample in samples.items():
            with self.subTest(method=method):
                variable_name = TRANSFER_METHODS[method]["workflow_variable"]
                context = SimpleNamespace(
                    input=Mock(),
                    logger=Mock(),
                    state=SimpleNamespace(current_step=None),
                    check_cancelled=Mock(),
                    cancel_event=threading.Event(),
                    wait=Mock(),
                )
                provider = SimpleNamespace(visible_texts=lambda: [sample])
                runner = WorkflowRunner(
                    context,
                    screen_state_provider=provider,
                    execution_context={"transfer_method": method},
                )
                runner.execute_step(switch, {variable_name: sample}, {})
                context.input.type_text.assert_called_once_with(sample)

    def test_runner_rejects_missing_unknown_and_unconfigured_methods(self):
        context = SimpleNamespace(
            input=Mock(), logger=Mock(), state=SimpleNamespace(), check_cancelled=Mock()
        )
        step = {
            "type": "switch_transfer_method",
            "branches": {"bank_account": [{"type": "key", "key": "HOME"}]},
        }
        for value in (None, "unknown"):
            runner = WorkflowRunner(context, execution_context={"transfer_method": value})
            with self.assertRaises(ValueError):
                runner.execute_step(step, {}, {})
        runner = WorkflowRunner(
            context, execution_context={"transfer_method": "phone_no"}
        )
        with self.assertRaisesRegex(ValueError, "no configured branch"):
            runner.execute_step(step, {}, {})

    def test_method_specific_normalization_preserves_leading_zeroes(self):
        bank_raw = withdrawal("bank_account")
        bank_raw.pop("mobile_no")
        mobile_raw = withdrawal("phone_no")
        bank = normalize_locked_withdrawal(bank_raw)
        mobile = normalize_locked_withdrawal(mobile_raw)
        self.assertEqual(bank["account_number"], "001234")
        self.assertEqual(bank["account_name"], "Recipient")
        self.assertEqual(bank["bank_name"], "Bank")
        self.assertNotIn("holder_name", bank)
        self.assertNotIn("mobile_no", bank)
        self.assertEqual(mobile["mobile_no"], "0123-4567")
        self.assertEqual(mobile["fps_phone_number"], "0123-4567")
        self.assertEqual(mobile["holder_name"], "收款人")
        self.assertEqual(mobile["transfer_method"], "phone_no")
        self.assertNotIn("bank_name", mobile)
        self.assertNotIn("account_name", mobile)
        self.assertNotIn("account_number", mobile)
        self.assertEqual(
            resolve_variables(
                switching_workflow(),
                {"fps_phone_number": mobile["fps_phone_number"]},
                execution_context={"transfer_method": "phone_no"},
            ),
            {"fps_phone_number": "0123-4567"},
        )
        email = normalize_locked_withdrawal(withdrawal("email"))
        fps_id = normalize_locked_withdrawal(withdrawal("fps_id"))
        self.assertEqual(email["fps_email"], "recipient@example.hk")
        self.assertEqual(fps_id["fps_id"], "0123456")
        self.assertEqual(email["holder_name"], "收款人")
        self.assertNotIn("account_name", email)
        self.assertTrue(
            text_verification_matches(
                ["收款人"], email["holder_name"], "account_name"
            )
        )

    def test_missing_unknown_and_missing_required_identifier_are_rejected(self):
        for raw in (
            withdrawal(None),
            withdrawal("unknown"),
            withdrawal("bank_account", account_number=None),
            withdrawal("bank_account", account_name=None),
            withdrawal("bank_account", bank_name=None),
            withdrawal("phone_no", mobile_no=None),
            withdrawal("phone_no", holder_name=None),
            withdrawal("email", mobile_no="not-an-email"),
            withdrawal("fps_id", mobile_no="12345678"),
        ):
            with self.assertRaises(ValueError):
                normalize_locked_withdrawal(raw)

    def test_type_and_transfer_method_combinations_are_explicit(self):
        for method in TRANSFER_METHODS:
            expected_type = "bank" if method == "bank_account" else "kuaizhuan"
            with self.subTest(method=method, expected_type=expected_type):
                self.assertEqual(
                    normalize_locked_withdrawal(withdrawal(method))["transfer_method"],
                    method,
                )
                incompatible_type = (
                    "kuaizhuan" if expected_type == "bank" else "bank"
                )
                with self.assertRaisesRegex(ValueError, "type does not match"):
                    normalize_locked_withdrawal(
                        withdrawal(method, type=incompatible_type)
                    )

    def test_phone_verification_requires_all_eight_digits(self):
        self.assertTrue(
            text_verification_matches(
                ["0123 4567"], "0123-4567", "fps_phone"
            )
        )
        self.assertFalse(
            text_verification_matches(["4567"], "0123-4567", "fps_phone")
        )

    def test_digit_only_keyboard_cannot_execute_email_branch(self):
        candidate = switching_workflow()
        candidate["keyboards"] = {
            "digits": {
                "id": "digits",
                "name": "Digits",
                "kind": "numeric",
                "keys": {
                    digit: {"x": index / 10, "y": 0.5}
                    for index, digit in enumerate("0123456789")
                },
            }
        }
        email_input = candidate["steps"][1]["branches"]["email"][0]
        email_input.update(input_method="screen_keyboard", keyboard_id="digits")
        validate_workflow(candidate)
        with self.assertRaisesRegex(ValueError, "cannot safely type"):
            require_transfer_method_branch(
                candidate,
                "email",
                recipient_identifier="recipient@example.hk",
            )

    def test_real_job_context_comes_only_from_protected_metadata(self):
        service = object.__new__(WorkflowService)
        service.start = Mock(return_value={"run_id": "run"})
        job = SimpleNamespace(
            workflow="flow_switch",
            device_id="phone_A",
            job_id="job_1",
            start_step_index=0,
            payload={},
            action="withdraw_v2",
            protected_metadata={"transfer_method": "phone_no"},
        )
        result = service.start_for_job(job, {"id": "flow_switch"})
        self.assertEqual(result, {"run_id": "run"})
        self.assertEqual(
            service.start.call_args.kwargs["execution_context"],
            {"transfer_method": "phone_no"},
        )

    def test_legacy_workflow_remains_valid(self):
        legacy = {
            "id": "flow_legacy",
            "name": "Legacy",
            "purpose": "payment",
            "version": 1,
            "variables": [],
            "keyboards": {},
            "financial": False,
            "steps": [{"id": "wait", "type": "wait", "seconds": 0}],
        }
        self.assertEqual(validate_workflow(legacy), legacy)

    def test_recorder_edits_each_explicit_method_branch(self):
        recorder = Recorder()
        owner = "browser-1"
        recorder.start(owner, "Transfer recording")
        recorder.start_alternate_screen(
            owner,
            None,
            source="withdrawal_api",
            transfer_method="bank_account",
        )
        condition_id = recorder.active_alternate_screen["condition_id"]
        for method, identifier in (
            ("bank_account", "account_number"),
            ("phone_no", "fps_phone_number"),
            ("email", "fps_email"),
            ("fps_id", "fps_id"),
        ):
            if method != "bank_account":
                recorder.start_alternate_screen(
                    owner,
                    None,
                    source="withdrawal_api",
                    transfer_method=method,
                    condition_id=condition_id,
                )
            recorder.workflow = recorder.prepare(
                owner,
                {
                    "type": "input",
                    "source": "variable",
                    "variable": identifier,
                },
                variable(identifier),
            )
            recorder.workflow = recorder.prepare(
                owner,
                {
                    "type": "verify_text",
                    "source": "variable",
                    "variable": identifier,
                    "match_mode": TRANSFER_METHODS[method]["verification_modes"][0],
                    "timeout_seconds": 2,
                },
            )
            recorder.finish_alternate_screen(owner)

        switch = recorder.workflow["steps"][0]
        self.assertEqual(switch["type"], "switch_transfer_method")
        self.assertEqual(set(switch["branches"]), set(TRANSFER_METHODS))
        self.assertEqual(
            [step["variable"] for step in switch["branches"]["phone_no"]],
            ["fps_phone_number", "fps_phone_number"],
        )
        validate_workflow(recorder.workflow)

    def test_recorder_can_save_only_the_phone_number_branch(self):
        recorder = Recorder()
        owner = "browser-1"
        recorder.start(owner, "Phone transfer only")
        recorder.start_alternate_screen(
            owner,
            None,
            source="withdrawal_api",
            transfer_method="phone_no",
        )
        recorder.workflow = recorder.prepare(
            owner,
            {
                "type": "input",
                "source": "variable",
                "variable": "fps_phone_number",
            },
            variable("fps_phone_number"),
        )
        recorder.workflow = recorder.prepare(
            owner,
            {
                "type": "verify_text",
                "source": "variable",
                "variable": "fps_phone_number",
                "match_mode": "fps_phone",
                "timeout_seconds": 2,
            },
        )
        recorder.finish_alternate_screen(owner)
        recorder.stop(owner)

        switch = recorder.workflow["steps"][0]
        self.assertEqual(set(switch["branches"]), {"phone_no"})
        validate_workflow(recorder.workflow)
        with tempfile.TemporaryDirectory() as temporary:
            saved = WorkflowStorage(temporary).save(recorder.workflow, create=True)
            self.assertEqual(
                set(saved["steps"][0]["branches"]),
                {"phone_no"},
            )
        require_transfer_method_branch(recorder.workflow, "phone_no")
        with self.assertRaisesRegex(ValueError, "no explicit branch"):
            require_transfer_method_branch(recorder.workflow, "bank_account")


if __name__ == "__main__":
    unittest.main()
