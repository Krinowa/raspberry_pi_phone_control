"""Synthetic UI fixtures only; never launches MAE or accepts real terms."""

import logging
import threading
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import Mock, patch

from android_control import Device
from automation.context import AutomationContext, Cancelled
from automation.runner import RunState
from automation.scripts.open_mae import button_center, click_button, run, PACKAGE, AGREE_RESOURCE_ID, PROCEED_RESOURCE_ID


DEVICE = Device("test-phone", 720, 1600, 0)


def hierarchy(**changes):
    root = ET.Element("hierarchy")
    ET.SubElement(root, "node", {
        "resource-id": "precheck_tnc_agree_button", "content-desc": "Agree",
        "package": PACKAGE, "clickable": "true", "enabled": "true",
        "bounds": "[112,1050][608,1134]", **changes,
    })
    return ET.tostring(root, encoding="unicode")


class OpenMaeTests(unittest.TestCase):
    def test_click_requires_both_attributes_on_the_same_node(self):
        for resource_id, description in ((AGREE_RESOURCE_ID, "Agree"),
                                         (PROCEED_RESOURCE_ID, "Proceed Anyway")):
            with self.subTest(resource_id=resource_id):
                context = Mock(device=DEVICE)
                correct = hierarchy(**{"resource-id": resource_id, "content-desc": description})
                self.assertTrue(click_button(context, correct, resource_id, description))
                context.send.assert_called_once_with("tap", (360, 1092))
                context.reset_mock()

                wrong_id = hierarchy(**{"resource-id": "other_button", "content-desc": description})
                self.assertFalse(click_button(context, wrong_id, resource_id, description))
                context.send.assert_not_called()

                for wrong_description in ("Other", "", description.lower()):
                    wrong_label = hierarchy(**{"resource-id": resource_id, "content-desc": wrong_description})
                    # A different node with the expected description cannot satisfy the check.
                    root = ET.fromstring(wrong_label)
                    root.append(ET.fromstring(wrong_id).find("node"))
                    with self.assertRaises(ValueError):
                        click_button(context, ET.tostring(root, encoding="unicode"), resource_id, description)
                    context.send.assert_not_called()

    def test_resource_id_and_current_bounds_select_the_tap(self):
        for resource_id in ("precheck_tnc_agree_button", f"{PACKAGE}:id/precheck_tnc_agree_button"):
            self.assertEqual(button_center(hierarchy(**{"resource-id": resource_id}), DEVICE, AGREE_RESOURCE_ID, "Agree"), (360, 1092))
        self.assertEqual(button_center(hierarchy(bounds="[20,100][220,180]"), DEVICE, AGREE_RESOURCE_ID, "Agree"), (120, 140))

    def test_label_alone_never_selects_an_unrelated_button(self):
        self.assertIsNone(button_center(hierarchy(**{"resource-id": "another_button"}), DEVICE, AGREE_RESOURCE_ID, "Agree"))

    def test_invalid_ambiguous_or_unavailable_target_never_tapped(self):
        invalid = ["not XML", hierarchy().replace("</hierarchy>", hierarchy()[11:])]
        for changes in ({"bounds": "bad"}, {"bounds": "[0,0][900,1800]"},
                        {"bounds": "[20,20][10,10]"}, {"clickable": "false"},
                        {"enabled": "false"}, {"visible-to-user": "false"},
                        {"content-desc": "Other"}, {"package": "other.app"}):
            invalid.append(hierarchy(**changes))
        for ui in invalid:
            with self.subTest(ui=ui), self.assertRaises(ValueError):
                button_center(ui, DEVICE, AGREE_RESOURCE_ID, "Agree")

    def test_script_sends_one_tap_through_context(self):
        context = Mock(device=DEVICE)
        context.dump_ui.return_value = hierarchy()
        run(context)
        taps = [call.args for call in context.send.call_args_list if call.args[0] == "tap"]
        self.assertEqual(taps, [("tap", (360, 1092))])
        self.assertEqual(context.dump_ui.call_count, 2)

    def test_absent_button_skips_tap(self):
        context = Mock(device=DEVICE)
        context.dump_ui.return_value = "<hierarchy/>"
        run(context)
        self.assertFalse(any(call.args[0] == "tap" for call in context.send.call_args_list))

    def test_cancellation_after_inspection_prevents_tap(self):
        event = threading.Event()
        controller = Mock()
        controller.device.return_value = DEVICE
        def inspect(device):
            event.set()
            return hierarchy()
        controller.dump_ui.side_effect = inspect
        context = AutomationContext(DEVICE, controller, event, logging.getLogger("test"), RunState("open_mae"))
        with patch.object(context, "wait"), self.assertRaises(Cancelled):
            run(context)
        self.assertFalse(any(call.args[1] == "tap" for call in controller.send.call_args_list))

    def test_proceed_resource_id_short_and_qualified(self):
        for resource_id in (PROCEED_RESOURCE_ID, f"{PACKAGE}:id/{PROCEED_RESOURCE_ID}"):
            ui = hierarchy(**{"resource-id": resource_id, "content-desc": "Proceed Anyway",
                              "bounds": "[42,1206][678,1290]"})
            self.assertEqual(button_center(ui, DEVICE, PROCEED_RESOURCE_ID, "Proceed Anyway"), (360, 1248))

    def test_both_buttons_use_separate_fresh_hierarchies(self):
        context = Mock(device=DEVICE)
        context.dump_ui.side_effect = [hierarchy(), hierarchy(**{
            "resource-id": PROCEED_RESOURCE_ID, "content-desc": "Proceed Anyway",
            "bounds": "[20,100][220,180]"})]
        run(context)
        taps = [call.args for call in context.send.call_args_list if call.args[0] == "tap"]
        self.assertEqual(taps, [("tap", (360, 1092)), ("tap", (120, 140))])
        self.assertEqual(context.dump_ui.call_count, 2)

    def test_proceed_wrong_label_is_rejected(self):
        with self.assertRaises(ValueError):
            button_center(hierarchy(**{"resource-id": PROCEED_RESOURCE_ID}),
                          DEVICE, PROCEED_RESOURCE_ID, "Proceed Anyway")

    def test_cancellation_during_second_inspection_prevents_proceed(self):
        event = threading.Event()
        controller = Mock()
        controller.device.return_value = DEVICE
        def inspect(device):
            if controller.dump_ui.call_count == 1:
                return hierarchy()
            event.set()
            return hierarchy(**{"resource-id": PROCEED_RESOURCE_ID,
                                "content-desc": "Proceed Anyway"})
        controller.dump_ui.side_effect = inspect
        context = AutomationContext(DEVICE, controller, event, logging.getLogger("test"), RunState("open_mae"))
        with patch.object(context, "wait"), self.assertRaises(Cancelled):
            run(context)
        taps = [call.args[1:] for call in controller.send.call_args_list if call.args[1] == "tap"]
        self.assertEqual(taps, [("tap", (360, 1092))])
