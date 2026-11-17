"""External transfer-method registry and restart-safety regression tests."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from automation.transfer_methods import (
    BUILTIN_TRANSFER_METHODS,
    TRANSFER_METHODS,
    TransferMethodConfigurationError,
    load_transfer_methods,
    transfer_method_definition,
    transfer_method_fingerprint,
    transfer_methods_config_path,
    validate_stored_transfer_method,
    validate_transfer_method,
)
from automation.integrations.withdraw_v2.journal import WithdrawJournal
from automation.integrations.withdraw_v2.normalization import (
    normalize_locked_withdrawal,
)
from automation.workflows.runner import WorkflowRunner
from automation.workflows.validation import (
    require_transfer_method_branch,
    resolve_variables,
    validate_workflow,
)


def variable(name):
    return {
        "name": name,
        "label": name,
        "type": "text",
        "required": True,
        "default": None,
    }


def branch(identifier):
    return [
        {
            "id": "input_" + identifier,
            "type": "input",
            "source": "variable",
            "variable": identifier,
        },
        {
            "id": "verify_" + identifier,
            "type": "verify_text",
            "source": "variable",
            "variable": identifier,
            "match_mode": "account_number",
            "timeout_seconds": 2,
        },
    ]


def workflow(branches):
    return {
        "id": "flow_config",
        "name": "Configured methods",
        "purpose": "payment",
        "version": 1,
        "variables": [variable("account_number"), variable("mobile_no")],
        "keyboards": {},
        "financial": False,
        "steps": [
            {"id": "before", "type": "key", "key": "HOME"},
            {
                "id": "method",
                "type": "switch_transfer_method",
                "branches": branches,
            },
            {"id": "after", "type": "key", "key": "BACK"},
        ],
    }


class TransferMethodConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "transfer_methods.json"

    def tearDown(self):
        load_transfer_methods(Path(self.temp.name) / "missing.json")

    def write(self, methods, schema_version=1):
        self.path.write_text(
            json.dumps({"schema_version": schema_version, "methods": methods}),
            encoding="utf-8",
        )

    def test_missing_file_keeps_builtins(self):
        load_transfer_methods(self.path)
        self.assertEqual(TRANSFER_METHODS, BUILTIN_TRANSFER_METHODS)

    def test_valid_bank_and_typed_phone_aliases_load(self):
        self.write(
            [
                {
                    "value": "duitnow_mobile",
                    "label": "DuitNow Mobile",
                    "identifier_profile": "fps_phone_number",
                },
                {
                    "value": "instant_bank",
                    "label": "Instant Bank Transfer",
                    "identifier_profile": "account_number",
                },
            ]
        )
        load_transfer_methods(self.path)
        self.assertEqual(
            transfer_method_definition("duitnow_mobile")["identifier_field"],
            "mobile_no",
        )
        self.assertEqual(
            transfer_method_definition("duitnow_mobile")["workflow_variable"],
            "fps_phone_number",
        )
        self.assertEqual(
            transfer_method_definition("instant_bank")["identifier_field"],
            "account_number",
        )
        self.assertEqual(
            normalize_locked_withdrawal(
                {
                    "id": 1,
                    "tx_id": "TX-1",
                    "order_id": "ORDER-1",
                    "final_amount": "10.00",
                    "transfer_method": "instant_bank",
                    "type": "bank",
                    "account_number": "001234",
                    "account_name": "Recipient",
                    "bank_name": "Bank",
                }
            )["account_number"],
            "001234",
        )

    def test_fpx_account_alias_preserves_unicode_label_and_ascii_value(self):
        self.write(
            [
                {
                    "value": "fpx",
                    "label": "FPX 即時轉賬",
                    "identifier_profile": "account_number",
                }
            ]
        )

        options = load_transfer_methods(self.path)
        configured = next(item for item in options if item["value"] == "fpx")

        self.assertEqual(configured["label"], "FPX 即時轉賬")
        self.assertEqual(configured["value"], "fpx")
        self.assertEqual(configured["identifier_field"], "account_number")

    def test_invalid_json_profile_duplicate_override_and_schema_are_rejected(self):
        invalid_documents = [
            "{",
            json.dumps(
                {
                    "schema_version": 2,
                    "methods": [],
                }
            ),
            json.dumps(
                {
                    "schema_version": 1,
                    "methods": [
                        {
                            "value": "email",
                            "label": "Email",
                            "identifier_profile": "email",
                        }
                    ],
                }
            ),
            json.dumps(
                {
                    "schema_version": 1,
                    "methods": [
                        {
                            "value": "bank_account",
                            "label": "Override",
                            "identifier_profile": "account_number",
                        }
                    ],
                }
            ),
            json.dumps(
                {
                    "schema_version": 1,
                    "methods": [
                        {
                            "value": "same",
                            "label": "First",
                            "identifier_profile": "account_number",
                        },
                        {
                            "value": "same",
                            "label": "Second",
                            "identifier_profile": "mobile_no",
                        },
                    ],
                }
            ),
            json.dumps(
                {
                    "schema_version": 1,
                    "methods": [
                        {
                            "value": "legacy_mobile_alias",
                            "label": "Legacy Mobile Alias",
                            "identifier_profile": "mobile_no",
                        }
                    ],
                }
            ),
        ]
        for document in invalid_documents:
            with self.subTest(document=document):
                self.path.write_text(document, encoding="utf-8")
                with self.assertRaises(TransferMethodConfigurationError):
                    load_transfer_methods(self.path)

    def test_existing_workflow_survives_but_new_method_requires_branch(self):
        self.write(
            [
                {
                    "value": "instant_bank",
                    "label": "Instant Bank",
                    "identifier_profile": "account_number",
                }
            ]
        )
        load_transfer_methods(self.path)
        existing = workflow(
            {
                "bank_account": branch("account_number"),
                "mobile_number": branch("mobile_no"),
            }
        )
        validate_workflow(existing)
        with self.assertRaisesRegex(ValueError, "no explicit branch"):
            require_transfer_method_branch(existing, "instant_bank")

    def test_configured_branch_executes_with_shared_steps(self):
        self.write(
            [
                {
                    "value": "instant_bank",
                    "label": "Instant Bank",
                    "identifier_profile": "account_number",
                }
            ]
        )
        load_transfer_methods(self.path)
        configured_branch = [
            {"id": "branch_key", "type": "key", "key": "RIGHT"},
            *branch("account_number"),
        ]
        configured_branch[1]["id"] = "input_instant_bank"
        configured_branch[2]["id"] = "verify_instant_bank"
        candidate = workflow(
            {
                "bank_account": branch("account_number"),
                "mobile_number": branch("mobile_no"),
                "instant_bank": configured_branch,
            }
        )
        validate_workflow(candidate)
        require_transfer_method_branch(candidate, "instant_bank")
        with self.assertRaisesRegex(ValueError, "account_number"):
            resolve_variables(
                candidate,
                {},
                execution_context={"transfer_method": "instant_bank"},
            )
        self.assertEqual(
            resolve_variables(
                candidate,
                {"account_number": "001234"},
                execution_context={"transfer_method": "instant_bank"},
            ),
            {"account_number": "001234"},
        )
        context = SimpleNamespace(
            input=Mock(),
            logger=Mock(),
            state=SimpleNamespace(current_step=None),
            check_cancelled=Mock(),
        )
        runner = WorkflowRunner(
            context,
            screen_state_provider=SimpleNamespace(visible_texts=lambda: ["001234"]),
            execution_context={"transfer_method": "instant_bank"},
        )
        for step in (
            candidate["steps"][0],
            candidate["steps"][1],
            candidate["steps"][2],
        ):
            runner.execute_step(
                step,
                {"account_number": "001234", "mobile_no": ""},
                {},
            )
        self.assertEqual(
            context.input.key.call_args_list,
            [call("HOME"), call("RIGHT"), call("BACK")],
        )

    def test_profile_change_is_rejected_for_persisted_work(self):
        self.write(
            [
                {
                    "value": "alias",
                    "label": "Alias",
                    "identifier_profile": "account_number",
                }
            ]
        )
        load_transfer_methods(self.path)
        fingerprint = transfer_method_fingerprint("alias")
        journal = WithdrawJournal(Path(self.temp.name) / "journal.json")
        journal.begin(
            "phone_A",
            phase="LOCKED",
            withdraw_id=1,
            tx_id="TX-1",
            transfer_method="alias",
            transfer_method_profile="account_number",
            transfer_method_fingerprint=fingerprint,
        )
        self.write(
            [
                {
                    "value": "alias",
                    "label": "Alias",
                    "identifier_profile": "fps_phone_number",
                }
            ]
        )
        load_transfer_methods(self.path)
        with self.assertRaisesRegex(ValueError, "incompatible|configuration changed"):
            validate_stored_transfer_method("alias", "account_number", fingerprint)
        with self.assertRaisesRegex(ValueError, "incompatible|configuration changed"):
            journal.validate_transfer_method_compatibility()

    def test_all_v213_methods_survive_journal_restart_validation(self):
        journal = WithdrawJournal(Path(self.temp.name) / "restart-journal.json")
        for index, method in enumerate(TRANSFER_METHODS, 1):
            definition = transfer_method_definition(method)
            journal.begin(
                f"phone_{index}",
                phase="LOCKED",
                withdraw_id=index,
                tx_id=f"TX-{index}",
                transfer_method=method,
                transfer_method_profile=definition["identifier_profile"],
                transfer_method_fingerprint=transfer_method_fingerprint(method),
            )
        WithdrawJournal(journal.path).validate_transfer_method_compatibility()

    def test_legacy_mobile_value_is_readable_but_not_live(self):
        legacy = workflow(
            {
                "bank_account": branch("account_number"),
                "mobile_number": branch("mobile_no"),
            }
        )
        self.assertEqual(validate_workflow(legacy), legacy)
        with self.assertRaisesRegex(ValueError, "missing or unsupported"):
            validate_transfer_method("mobile_number")

    def test_development_and_frozen_launcher_path_conventions(self):
        override = Path(self.temp.name) / "override.json"
        self.assertEqual(
            transfer_methods_config_path(
                {"PHONE_TRANSFER_METHODS_FILE": str(override)}
            ),
            override.resolve(),
        )
        data = Path(self.temp.name) / "Phone Controller"
        self.assertEqual(
            transfer_methods_config_path({"PHONE_APP_DATA_DIR": str(data)}),
            data.resolve() / "config" / "transfer_methods.json",
        )
        local = Path(self.temp.name) / "LocalAppData"
        with patch("windows_runtime.os", SimpleNamespace(name="nt")):
            self.assertEqual(
                transfer_methods_config_path({"LOCALAPPDATA": str(local)}),
                local / "Phone Controller" / "config" / "transfer_methods.json",
            )
            with self.assertRaisesRegex(RuntimeError, "LOCALAPPDATA is unavailable"):
                transfer_methods_config_path(
                    {"XDG_DATA_HOME": str(Path(self.temp.name) / "ignored-xdg")}
                )

        xdg = Path(self.temp.name) / "xdg"
        with patch("windows_runtime.os", SimpleNamespace(name="posix")):
            self.assertEqual(
                transfer_methods_config_path({"XDG_DATA_HOME": str(xdg)}),
                xdg / "phone-controller" / "config" / "transfer_methods.json",
            )


if __name__ == "__main__":
    unittest.main()
