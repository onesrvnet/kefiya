# -*- coding: utf-8 -*-
# Copyright (c) 2026, phamos GmbH and contributors
# For license information, please see license.txt

import contextlib
import json
import logging
import threading
from unittest.mock import MagicMock, patch

import frappe
import requests
from fints.client import NeedTANResponse
from fints.models import SEPAAccount
from fints.exceptions import (
    FinTSClientError,
    FinTSClientPINError,
    FinTSClientTemporaryAuthError,
    FinTSConnectionError,
    FinTSSCARequiredError,
)
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_days, getdate

from kefiya.utils import fints_controller, fints_controller_legacy
from kefiya.utils.fints_controller import FetchRefused, FetchTanRequired
from kefiya.utils.fints_controller_legacy import TanAuthenticationRequired

CONTROLLERS = (fints_controller.FinTSController, fints_controller_legacy.FinTSController)


def make_controller(cls):
    """Build a controller without its __init__, which logs in to the bank."""
    controller = cls.__new__(cls)
    controller.kefiya_login = frappe._dict(
        name="DKB Test", account_iban="DE02120300000000202051"
    )
    controller.fints_connection = MagicMock()
    controller.fints_connection._standing_dialog = None
    controller.get_fints_account_by_iban = MagicMock()
    controller.interactive = MagicMock()
    return controller


class TestFetchTanRequired(FrappeTestCase):
    def test_tan_on_fetch_raises_clear_error(self):
        start = add_days(getdate(), -120)
        for cls in CONTROLLERS:
            with self.subTest(controller=cls.__module__):
                controller = make_controller(cls)
                controller.fints_connection.get_transactions.return_value = MagicMock(
                    spec=NeedTANResponse
                )
                with self.assertRaises(FetchTanRequired):
                    controller.get_fints_transactions(start, add_days(getdate(), -1))

    def test_sca_required_error_on_fetch_raises_clear_error(self):
        start = add_days(getdate(), -120)
        for cls in CONTROLLERS:
            with self.subTest(controller=cls.__module__):
                controller = make_controller(cls)
                controller.fints_connection.get_transactions.side_effect = (
                    FinTSSCARequiredError("SCA required")
                )
                with self.assertRaises(FetchTanRequired):
                    controller.get_fints_transactions(start, add_days(getdate(), -1))

    def test_import_passes_tan_error_through_unwrapped(self):
        for cls in CONTROLLERS:
            with self.subTest(controller=cls.__module__):
                controller = make_controller(cls)
                controller.get_fints_transactions = MagicMock(
                    side_effect=FetchTanRequired("TAN needed")
                )
                with patch("frappe.get_doc"), self.assertRaises(FetchTanRequired):
                    controller.import_fints_transactions("KEFIYA-IMPORT-TEST")


# What fints raises when DKB rejects HKSPA, shortened
DKB_REJECTION = (
    "Error response: fints.message.FinTSInstituteMessage([...responses=["
    "fints.formals.Response(code='9800', reference_element=None, text='Dialog abgebrochen.'), "
    "fints.formals.Response(code='9210', reference_element=None, text='Auftrag abgelehnt')]"
)


class TestLegacyTanAuthenticationRequired(FrappeTestCase):
    def setUp(self):
        # log_error commits, so it would keep test entries in the Error Log
        log_error = patch("frappe.log_error")
        log_error.start()
        self.addCleanup(log_error.stop)

    def get_accounts(self, side_effect):
        controller = make_controller(fints_controller_legacy.FinTSController)
        controller.fints_connection.get_sepa_accounts.side_effect = side_effect
        controller._FinTSController__get_fints_accounts()

    def test_rejected_account_list_offers_tan_and_retry(self):
        with self.assertRaises(TanAuthenticationRequired):
            self.get_accounts(Exception(DKB_REJECTION))
        message = frappe.message_log[-1]
        self.assertEqual(
            message["primary_action"]["client_action"],
            "kefiya.interactive.use_tan_and_try_again",
        )
        self.assertEqual(message["primary_action"]["args"], "DKB Test")
        self.assertIn("DKB Test", message["message"])
        self.assertIn("9800 Dialog abgebrochen., 9210 Auftrag abgelehnt", message["message"])

    def test_no_button_without_write_access(self):
        with patch("frappe.has_permission", return_value=False), self.assertRaises(
            TanAuthenticationRequired
        ):
            self.get_accounts(Exception(DKB_REJECTION))
        message = frappe.message_log[-1]
        self.assertNotIn("primary_action", message)
        self.assertIn("Ask someone who can edit this Kefiya Login", message["message"])

    def test_http_400_on_account_list_asks_for_tan(self):
        with self.assertRaises(TanAuthenticationRequired):
            self.get_accounts(FinTSConnectionError("Bad status code 400"))
        self.assertIn("Bank answer: HTTP 400", frappe.message_log[-1]["message"])

    def test_failed_account_list_skips_the_dialog_end(self):
        # fints would send HKEND on exit; it fails too and hides the message
        for error in (FinTSConnectionError("Bad status code 400"), Exception("code='9942'")):
            with self.subTest(error=str(error)):
                controller = make_controller(fints_controller_legacy.FinTSController)
                # the dialog fints opened for the account list
                controller.fints_connection._standing_dialog = MagicMock(open=True)
                controller.fints_connection.get_sepa_accounts.side_effect = error
                with self.assertRaises(frappe.ValidationError):
                    controller._FinTSController__get_fints_accounts()
                self.assertFalse(controller.fints_connection._standing_dialog.open)

    def test_sca_required_error_asks_for_tan(self):
        with self.assertRaises(TanAuthenticationRequired):
            self.get_accounts(FinTSSCARequiredError("SCA required"))

    def test_tan_at_dialog_start_asks_for_tan(self):
        controller = make_controller(fints_controller_legacy.FinTSController)
        controller.fints_connection.init_tan_response = MagicMock(spec=NeedTANResponse)
        with self.assertRaises(TanAuthenticationRequired):
            controller._FinTSController__init_tan_processing()

    def test_other_errors_keep_the_bank_message(self):
        with self.assertRaises(frappe.ValidationError) as ctx:
            self.get_accounts(Exception("code='9942' PIN falsch"))
        self.assertNotIsInstance(ctx.exception, TanAuthenticationRequired)


class TestTanAuthenticationPerLogin(FrappeTestCase):
    def test_controller_follows_the_login(self):
        from kefiya.utils import client

        for enabled, module in ((1, fints_controller), (0, fints_controller_legacy)):
            with self.subTest(enabled=enabled), patch(
                "frappe.db.get_value", return_value=enabled
            ) as get_value:
                self.assertIs(client._get_fints_controller("DKB Test"), module.FinTSController)
                get_value.assert_called_with(
                    "Kefiya Login", "DKB Test", "enable_tan_authentication"
                )

    def test_new_login_uses_tan_by_default(self):
        login = frappe.new_doc("Kefiya Login")
        self.assertEqual(login.enable_tan_authentication, 1)


class TestMoveTanAuthenticationPatch(FrappeTestCase):
    def run_patch(self, site_value):
        from kefiya.patches.v0_5_0 import move_tan_authentication_to_kefiya_login as move

        frappe.db.delete(
            "Singles", {"doctype": "Kefiya Settings", "field": "enable_tan_authentication"}
        )
        frappe.db.sql(
            "insert into `tabSingles` (doctype, field, value) values (%s, %s, %s)",
            ("Kefiya Settings", "enable_tan_authentication", site_value),
        )
        # reload_doc can commit; the schema is already synced in tests
        with patch("frappe.reload_doc"):
            move.execute()

    def test_second_run_keeps_the_logins(self):
        if not frappe.db.count("Kefiya Login"):
            self.skipTest("no Kefiya Login on this site")
        from kefiya.patches.v0_5_0 import move_tan_authentication_to_kefiya_login as move

        self.run_patch("1")
        with patch("frappe.reload_doc"):
            move.execute()
        values = set(frappe.get_all("Kefiya Login", pluck="enable_tan_authentication"))
        self.assertEqual(values, {1})

    def test_logins_get_the_site_setting(self):
        if not frappe.db.count("Kefiya Login"):
            self.skipTest("no Kefiya Login on this site")
        for site_value, expected in (("1", 1), ("0", 0)):
            with self.subTest(site_value=site_value):
                self.run_patch(site_value)
                values = set(frappe.get_all("Kefiya Login", pluck="enable_tan_authentication"))
                self.assertEqual(values, {expected})
                self.assertFalse(
                    frappe.db.exists(
                        "Singles",
                        {"doctype": "Kefiya Settings", "field": "enable_tan_authentication"},
                    )
                )


def bank_answers_400(client):
    """Mimic fints.connection: log the message, get HTTP 400 back, raise."""
    def get_sepa_accounts():
        logging.getLogger("fints.connection").debug("Sending HKSPA:1:3")
        response = MagicMock(ok=False, status_code=400, url="https://banking.example/fints", text="Ungültige Nachricht")
        for hook in client.connection.session.hooks["response"]:
            hook(response)
        raise FinTSConnectionError("Bad status code 400")
    return get_sepa_accounts


class TestBankTrafficLog(FrappeTestCase):
    def setUp(self):
        self.client = MagicMock()
        self.client.connection.session = requests.Session()

    def test_http_error_and_traffic_go_to_error_log(self):
        with patch("frappe.log_error") as log_error, self.assertRaises(FinTSConnectionError):
            with fints_controller.log_bank_traffic_on_error(self.client, "Kefiya: Could not load accounts"):
                bank_answers_400(self.client)()

        message = log_error.call_args.kwargs["message"]
        self.assertIn("HTTP 400 from https://banking.example/fints", message)
        self.assertIn("Ungültige Nachricht", message)
        self.assertIn("Sending HKSPA:1:3", message)
        self.assertEqual(self.client.connection.session.hooks["response"], [])

    def test_other_threads_are_not_recorded(self):
        def other_request():
            logging.getLogger("fints.connection").debug("Sending for another login")

        with patch("frappe.log_error") as log_error, self.assertRaises(FinTSConnectionError):
            with fints_controller.log_bank_traffic_on_error(self.client, "Kefiya: Could not load accounts"):
                thread = threading.Thread(target=other_request)
                thread.start()
                thread.join()
                bank_answers_400(self.client)()

        message = log_error.call_args.kwargs["message"]
        self.assertIn("Sending HKSPA:1:3", message)
        self.assertNotIn("another login", message)
        self.assertEqual(logging.getLogger("fints").level, logging.NOTSET)

    def test_tan_interaction_is_not_logged(self):
        with patch("frappe.log_error") as log_error, self.assertRaises(fints_controller.TanInteractionRequired):
            with fints_controller.log_bank_traffic_on_error(self.client, "Kefiya: Could not load accounts"):
                raise fints_controller.TanInteractionRequired()

        log_error.assert_not_called()


class TestAccountsBankRefusal(FrappeTestCase):
    def test_bank_reason_and_error_log_link_replace_the_400(self):
        # DKB ends the dialog with 9800/9040; fints goes on and gets HTTP 400
        controller = make_controller(fints_controller.FinTSController)
        controller.kefiya_login = frappe._dict(iban_list=None)
        controller.fints_connection.connection.session = requests.Session()
        controller.fints_connection.init_tan_response = None

        def get_sepa_accounts():
            for code, text in (("9800", "Dialog abgebrochen."), ("9040", "Bitte resynchronisiere deinen chipTAN-Generator.")):
                logging.getLogger("fints.client").error(
                    "Dialog response: %s - %s", code, text,
                    extra={"fints_response_code": code, "fints_response_text": text},
                )
            raise FinTSConnectionError("Bad status code 400")
        controller.fints_connection.get_sepa_accounts.side_effect = get_sepa_accounts

        with patch("frappe.log_error", return_value=frappe._dict(name="ERR-0001")), \
                self.assertRaises(frappe.ValidationError) as ctx:
            controller._FinTSController__fetch_fints_accounts()

        message = str(ctx.exception)
        self.assertIn("9040: Bitte resynchronisiere deinen chipTAN-Generator.", message)
        self.assertNotIn("Bad status code 400", message)
        self.assertIn("chipTAN generator needs to be resynchronised", message)
        self.assertIn('href="/desk/error-log/ERR-0001"', message)


def bank_responds(code, text, exc):
    """Mimic fints: log the bank's 9xxx response, then raise."""
    def get_transactions(*args):
        logging.getLogger("fints.client").error(
            "Dialog response: %s - %s", code, text,
            extra={"fints_response_code": code, "fints_response_text": text},
        )
        raise exc
    return get_transactions


class TestFetchBankRefusal(FrappeTestCase):
    def test_9010_shows_the_bank_reason_instead_of_bpd(self):
        # fints says "could not fetch BPD" for any 9010, also on the fetch
        for cls in CONTROLLERS:
            with self.subTest(controller=cls.__module__):
                controller = make_controller(cls)
                controller.fints_connection.connection.session = requests.Session()
                controller.fints_connection.get_transactions.side_effect = bank_responds(
                    "9010", "Zeitraum nicht zulässig",
                    FinTSClientError("Error during dialog initialization, could not fetch BPD."),
                )
                with patch("frappe.log_error", return_value=frappe._dict(name="ERR-0002")), \
                        self.assertRaises(FetchRefused) as ctx:
                    controller.get_fints_transactions(add_days(getdate(), -400), add_days(getdate(), -1))

                message = str(ctx.exception)
                self.assertIn("9010: Zeitraum nicht zulässig", message)
                self.assertNotIn("BPD", message)
                self.assertIn("choose a later one", message)
                self.assertIn('href="/desk/error-log/ERR-0002"', message)

    def test_errors_without_bank_reason_pass_through(self):
        for cls in CONTROLLERS:
            with self.subTest(controller=cls.__module__):
                controller = make_controller(cls)
                controller.fints_connection.connection.session = requests.Session()
                controller.fints_connection.get_transactions.side_effect = FinTSClientPINError("PIN wrong?")
                with patch("frappe.log_error"), self.assertRaises(FinTSClientPINError):
                    controller.get_fints_transactions(add_days(getdate(), -30), add_days(getdate(), -1))

    def test_tan_on_fetch_is_not_logged(self):
        controller = make_controller(fints_controller.FinTSController)
        controller.fints_connection.connection.session = requests.Session()
        controller.fints_connection.get_transactions.side_effect = FinTSSCARequiredError("SCA required")
        with patch("frappe.log_error") as log_error, self.assertRaises(FetchTanRequired):
            controller.get_fints_transactions(add_days(getdate(), -120), add_days(getdate(), -1))
        log_error.assert_not_called()

    def test_import_passes_the_refusal_through_unwrapped(self):
        for cls in CONTROLLERS:
            with self.subTest(controller=cls.__module__):
                controller = make_controller(cls)
                controller.get_fints_transactions = MagicMock(side_effect=FetchRefused("refused"))
                with patch("frappe.get_doc"), self.assertRaises(FetchRefused):
                    controller.import_fints_transactions("KEFIYA-IMPORT-TEST")


class TestTanChallenge(FrappeTestCase):
    def test_qr_image_and_escaped_text(self):
        response = MagicMock(
            spec=NeedTANResponse,
            challenge="Scan the code <now>",
            challenge_html=None,
            challenge_matrix=("image/png", b"\x89PNG"),
        )
        challenge = fints_controller.tan_challenge(response)
        self.assertEqual(challenge["text"], "Scan the code &lt;now&gt;")
        self.assertEqual(challenge["image"], "data:image/png;base64,iVBORw==")

    def test_non_image_matrix_is_dropped(self):
        for mime_type in ("text/html", 'image/png" onerror="alert(1)'):
            with self.subTest(mime_type=mime_type):
                response = MagicMock(
                    spec=NeedTANResponse,
                    challenge=None,
                    challenge_html=None,
                    challenge_matrix=(mime_type, b"<script>"),
                )
                self.assertIsNone(fints_controller.tan_challenge(response))

    def test_structured_text_comes_from_challenge_html(self):
        response = MagicMock(
            spec=NeedTANResponse,
            challenge="<b>Betrag</b>",
            challenge_html="<b>Betrag</b>",
            challenge_matrix=None,
        )
        self.assertEqual(fints_controller.tan_challenge(response), {"text": "<b>Betrag</b>"})

    def test_ask_for_tan_sends_the_challenge(self):
        for decoupled, request in ((False, "request_tan"), (True, "request_mfa_confirmation")):
            with self.subTest(decoupled=decoupled):
                controller = make_controller(fints_controller.FinTSController)
                controller._FinTSController__persist_fints_state = MagicMock()
                response = MagicMock(
                    spec=NeedTANResponse,
                    decoupled=decoupled,
                    challenge="Scan the code",
                    challenge_html=None,
                    challenge_matrix=None,
                )
                controller.ask_for_tan(response)
                kwargs = getattr(controller.interactive, request).call_args.kwargs
                self.assertEqual(kwargs["challenge"], {"text": "Scan the code"})


class TestResolveTanInteraction(FrappeTestCase):
    """The form offers the accounts once the TAN is resolved."""

    def resolve(self, controller):
        from kefiya.utils import client

        with patch.object(client, "_use_tan_authentication", return_value=True), \
                patch.object(fints_controller, "FinTSController", controller):
            return client.resolve_tan_interaction(
                "DKB phamos", {"mfa_confirmation": 1, "tan": "123456"}
            )

    def test_accepted_tan_returns_the_accounts(self):
        controller = MagicMock()
        controller.return_value.get_fints_accounts.return_value = [{"iban": "DE02120300000000202051"}]
        self.assertEqual(
            self.resolve(controller),
            {"tan_resolved": True, "accounts": [{"iban": "DE02120300000000202051"}]},
        )

    def test_further_interaction_is_reported(self):
        controller = MagicMock(side_effect=fints_controller.TanInteractionRequired())
        self.assertEqual(self.resolve(controller), {"tan_resolved": False})


class TestLoginRejected(FrappeTestCase):
    def test_wrong_pin_at_dialog_start_is_explained(self):
        for cls in CONTROLLERS:
            with self.subTest(controller=cls.__module__):
                kefiya_login = MagicMock(stored_client_blob=None, stored_tan_blob=None)
                kefiya_login.name = "DKB Test"
                fints_client = MagicMock()
                fints_client.__enter__.side_effect = FinTSClientPINError("Error during dialog initialization, PIN wrong?")
                fints_client.fetch_tan_mechanisms.side_effect = FinTSClientPINError("Error during dialog initialization, PIN wrong?")
                fints_client.get_current_tan_mechanism.return_value = None
                with patch("frappe.get_doc", return_value=kefiya_login), \
                        patch("frappe.has_permission"), \
                        patch("frappe.log_error"), \
                        patch(cls.__module__ + ".FinTS3PinTanClient", return_value=fints_client), \
                        self.assertRaises(fints_controller.LoginRejected) as ctx:
                    cls("DKB Test")
                message = str(ctx.exception)
                self.assertIn("did not accept the login of Kefiya Login <strong>DKB Test</strong>", message)
                self.assertNotIn("PIN wrong?", message)

    def test_locked_access_is_explained(self):
        with self.assertRaises(fints_controller.LoginRejected) as ctx, \
                fints_controller.explain_login_rejection("DKB Test"):
            raise FinTSClientTemporaryAuthError("Account is temporarily locked.")
        self.assertIn("temporarily locked the online banking access", str(ctx.exception))

    def test_account_list_shows_the_bank_reason(self):
        controller = make_controller(fints_controller.FinTSController)
        controller.name = "DKB Test"
        controller.kefiya_login = frappe._dict(iban_list=None)
        controller.fints_connection.connection.session = requests.Session()

        def enter():
            logging.getLogger("fints.client").error(
                "Dialog response: 9931 - Anmeldung gesperrt.",
                extra={"fints_response_code": "9931", "fints_response_text": "Anmeldung gesperrt."},
            )
            raise FinTSClientPINError("Error during dialog initialization, PIN wrong?")
        controller.fints_connection.__enter__.side_effect = enter

        with patch("frappe.log_error", return_value=frappe._dict(name="ERR-0002")), \
                self.assertRaises(fints_controller.LoginRejected) as ctx:
            controller._FinTSController__fetch_fints_accounts()

        message = str(ctx.exception)
        self.assertIn("Check the FinTS login name and the PIN", message)
        self.assertIn("9931: Anmeldung gesperrt.", message)
        self.assertIn('href="/desk/error-log/ERR-0002"', message)


def login_with_pending_tan(decoupled=False):
    """A Kefiya Login whose last TAN prompt is still stored."""
    kefiya_login = MagicMock(
        stored_client_blob=None,
        stored_tan_blob=b"tan request",
        stored_dialog_blob=b"paused dialog",
        stored_tan_state_decoupled=decoupled,
        failed_connection=0,
    )
    kefiya_login.name = "DKB Test"
    return kefiya_login


def fints_client_with_tan_mode():
    """A fints client whose TAN mode and medium are already set up."""
    fints_client = MagicMock()
    fints_client.get_current_tan_mechanism.return_value = "921"
    fints_client.selected_tan_medium = ""
    fints_client.connection.session = requests.Session()
    fints_client._standing_dialog = None
    return fints_client


class TestPendingTan(FrappeTestCase):
    def open_controller(self, kefiya_login, fints_client, **kwargs):
        cls = fints_controller.FinTSController

        def persist(*args, **kw):
            kefiya_login.stored_tan_blob = None

        with patch("frappe.get_doc", return_value=kefiya_login), \
                patch("frappe.has_permission"), \
                patch("kefiya.utils.fints_controller.FinTS3PinTanClient", return_value=fints_client), \
                patch("kefiya.utils.fints_controller.NeedRetryResponse") as need_retry, \
                patch.object(cls, "_FinTSController__persist_fints_state", side_effect=persist) as persist_state, \
                patch.object(cls, "_FinTSController__fetch_fints_accounts", return_value=True):
            cls("DKB Test", **kwargs)
        return need_retry, persist_state

    def test_unanswered_tan_prompt_is_not_sent_to_the_bank(self):
        # e.g. loading the accounts again after the TAN prompt was closed, or
        # a background job that stored a TAN request nobody saw
        fints_client = fints_client_with_tan_mode()
        _, persist_state = self.open_controller(login_with_pending_tan(), fints_client)

        fints_client.resume_dialog.assert_not_called()
        fints_client.send_tan.assert_not_called()
        persist_state.assert_called_once_with()

    def test_answer_to_the_tan_prompt_resumes_the_dialog(self):
        fints_client = fints_client_with_tan_mode()
        fints_client.send_tan.return_value = None
        need_retry, _ = self.open_controller(login_with_pending_tan(), fints_client, tan="123456")

        fints_client.resume_dialog.assert_called_once_with(b"paused dialog")
        fints_client.send_tan.assert_called_once_with(need_retry.from_data.return_value, "123456")

    def test_rejected_tan_shows_the_bank_reason_and_drops_the_request(self):
        fints_client = fints_client_with_tan_mode()

        def send_tan(*args):
            logging.getLogger("fints.client").error(
                "Dialog response: 9942 - PIN/TAN falsch.",
                extra={"fints_response_code": "9942", "fints_response_text": "PIN/TAN falsch."},
            )
            raise FinTSClientPINError("Error during dialog initialization, PIN wrong?")
        fints_client.send_tan.side_effect = send_tan

        with patch("frappe.log_error", return_value=frappe._dict(name="ERR-0003")), \
                patch("kefiya.utils.fints_controller.forget_pending_tan") as forget, \
                self.assertRaises(fints_controller.LoginRejected) as ctx:
            self.open_controller(login_with_pending_tan(), fints_client, tan="123456")

        message = str(ctx.exception)
        self.assertIn("9942: PIN/TAN falsch.", message)
        self.assertIn("rejected the TAN", message)
        self.assertIn('href="/desk/error-log/ERR-0003"', message)
        forget.assert_called_once_with("DKB Test")

    def test_forget_pending_tan_commits_the_cleared_state(self):
        with patch("frappe.db.get_value", return_value="encrypted"), \
                patch("frappe.db.set_value") as set_value, \
                patch("frappe.db.commit") as commit:
            fints_controller.forget_pending_tan("DKB Test")

        values = set_value.call_args.args[2]
        self.assertIsNone(values["stored_tan_state"])
        self.assertIsNone(values["stored_dialog_state"])
        commit.assert_called_once_with()

    def test_forget_pending_tan_without_a_request_does_not_commit(self):
        with patch("frappe.db.get_value", return_value=None), \
                patch("frappe.db.set_value") as set_value, \
                patch("frappe.db.commit") as commit:
            fints_controller.forget_pending_tan("DKB Test")

        set_value.assert_not_called()
        commit.assert_not_called()


ACCOUNT = SEPAAccount(
    iban="DE02120300000000202051", bic="BYLADEM1001",
    accountnumber="202051", subaccount=None, blz="12030000",
)


class TestAccountsAfterTan(FrappeTestCase):
    """The TAN only unlocks the dialog it was asked in."""

    def answer_tan_prompt(self, fints_client, resume_method):
        cls = fints_controller.FinTSController
        kefiya_login = login_with_pending_tan()
        kefiya_login.iban_list = None
        self.dialog_ended = False

        @contextlib.contextmanager
        def resume_dialog(dialog_blob):
            fints_client._standing_dialog = "resumed dialog"
            yield fints_client
            fints_client._standing_dialog = None
            self.dialog_ended = True
        fints_client.resume_dialog.side_effect = resume_dialog

        def persist(*args, **kw):
            kefiya_login.stored_tan_blob = None

        with patch("frappe.get_doc", return_value=kefiya_login), \
                patch("frappe.has_permission"), \
                patch("kefiya.utils.fints_controller.FinTS3PinTanClient", return_value=fints_client), \
                patch("kefiya.utils.fints_controller.NeedRetryResponse") as need_retry, \
                patch.object(cls, "_FinTSController__persist_fints_state", side_effect=persist):
            need_retry.from_data.return_value.resume_method = resume_method
            controller = cls("DKB Test", tan="123456")
        return controller, kefiya_login

    def test_accounts_load_in_the_dialog_the_tan_unlocked(self):
        # A new dialog would start with another HKTAN, and the bank would ask
        # for the next TAN
        fints_client = fints_client_with_tan_mode()
        fints_client.send_tan.return_value = MagicMock()
        dialog_at_request = []

        def get_sepa_accounts():
            dialog_at_request.append(fints_client._standing_dialog)
            return [ACCOUNT]
        fints_client.get_sepa_accounts.side_effect = get_sepa_accounts

        controller, kefiya_login = self.answer_tan_prompt(fints_client, "_continue_dialog_initialization")

        self.assertEqual(dialog_at_request, ["resumed dialog"])
        fints_client.__enter__.assert_not_called()
        self.assertEqual(controller.get_fints_accounts(), [ACCOUNT])
        self.assertEqual(kefiya_login.iban_list, json.dumps([ACCOUNT.iban]))

        # an import runs in it too, so the dialog ends only with the controller
        self.assertFalse(self.dialog_ended)
        controller.close()
        self.assertTrue(self.dialog_ended)

    def test_tan_for_the_accounts_answers_with_the_accounts(self):
        fints_client = fints_client_with_tan_mode()
        fints_client.send_tan.return_value = [ACCOUNT]

        controller, kefiya_login = self.answer_tan_prompt(fints_client, "_get_sepa_accounts")

        fints_client.get_sepa_accounts.assert_not_called()
        fints_client.__enter__.assert_not_called()
        self.assertEqual(controller.get_fints_accounts(), [ACCOUNT])
        self.assertEqual(kefiya_login.iban_list, json.dumps([ACCOUNT.iban]))

    def test_without_open_dialog_the_client_opens_one(self):
        controller = make_controller(fints_controller.FinTSController)
        controller.fints_connection.init_tan_response = None

        with controller.trusted_client_context() as client:
            self.assertIs(client, controller.fints_connection)

        controller.fints_connection.__enter__.assert_called_once_with()
        controller.fints_connection.__exit__.assert_called_once()

    def test_import_runs_in_the_dialog_the_tan_unlocked(self):
        fints_client = fints_client_with_tan_mode()
        fints_client.send_tan.return_value = MagicMock()
        fints_client.get_sepa_accounts.return_value = [ACCOUNT]
        dialog_at_fetch = []

        def get_transactions(*args):
            dialog_at_fetch.append(fints_client._standing_dialog)
            return []
        fints_client.get_transactions.side_effect = get_transactions

        controller, kefiya_login = self.answer_tan_prompt(fints_client, "_continue_dialog_initialization")
        kefiya_login.account_iban = ACCOUNT.iban
        with controller:
            controller.get_fints_transactions()

        self.assertEqual(dialog_at_fetch, ["resumed dialog"])
        fints_client.__enter__.assert_not_called()
        self.assertTrue(self.dialog_ended)


class TestImportWithTan(FrappeTestCase):
    def test_tan_prompt_ends_the_import_call_quietly(self):
        from kefiya.utils import client

        controller = MagicMock(side_effect=fints_controller.TanInteractionRequired())
        with patch.object(client, "_use_tan_authentication", return_value=True), \
                patch.object(fints_controller, "FinTSController", controller):
            self.assertIsNone(client.import_fints_transactions("KIMP-0001", "DKB phamos", "KIMP-0001"))

        interactive = controller.call_args.args[1]
        self.assertEqual(interactive["kefiya_import"], "KIMP-0001")

    def test_tan_prompt_names_the_login_and_the_import(self):
        interactive = fints_controller.FinTSInteractive(
            {"docname": "KIMP-0001", "enabled": True, "kefiya_import": "KIMP-0001"},
            kefiya_login="DKB phamos",
        )
        with patch("frappe.publish_realtime") as publish:
            interactive.request_tan(challenge=None)

        params = publish.call_args.args[1]
        self.assertEqual(params["kefiya_login"], "DKB phamos")
        self.assertEqual(params["kefiya_import"], "KIMP-0001")

    def test_import_runs_with_the_tan_before_the_dialog_ends(self):
        from kefiya.utils import client

        steps = []
        controller = MagicMock()
        controller.return_value.import_fints_transactions.side_effect = \
            lambda name: steps.append("import " + name) or {"payments": []}
        controller.return_value.__exit__.side_effect = lambda *a: steps.append("dialog ended")

        with patch.object(client, "_use_tan_authentication", return_value=True), \
                patch.object(client, "pending_import", return_value="KIMP-0001"), \
                patch.object(fints_controller, "FinTSController", controller):
            result = client.resolve_tan_interaction(
                "DKB phamos", {"mfa_confirmation": 1, "tan": "123456", "kefiya_import": "KIMP-0001"}
            )

        self.assertEqual(steps, ["import KIMP-0001", "dialog ended"])
        self.assertEqual(result["import"], {"payments": []})

    def test_only_a_waiting_import_of_this_login_runs(self):
        from kefiya.utils import client

        rows = {
            "same login, draft": frappe._dict(kefiya_login="DKB phamos", docstatus=0),
            "other login": frappe._dict(kefiya_login="Other", docstatus=0),
            "already submitted": frappe._dict(kefiya_login="DKB phamos", docstatus=1),
        }
        for case, row in rows.items():
            with self.subTest(case), \
                    patch("frappe.has_permission"), \
                    patch("frappe.db.get_value", return_value=row):
                expected = "KIMP-0001" if case == "same login, draft" else None
                self.assertEqual(client.pending_import("KIMP-0001", "DKB phamos"), expected)

        self.assertIsNone(client.pending_import(None, "DKB phamos"))

    def test_fetch_uses_an_open_dialog(self):
        fints_client = MagicMock()
        fints_client._standing_dialog = "open dialog"
        fints_client.connection.session = requests.Session()
        fints_client.get_transactions.return_value = []

        fints_controller.fetch_fints_transactions(fints_client, ACCOUNT, None, None)

        fints_client.__enter__.assert_not_called()

    def test_fetch_without_open_dialog_opens_one(self):
        fints_client = MagicMock()
        fints_client._standing_dialog = None
        fints_client.connection.session = requests.Session()
        fints_client.get_transactions.return_value = []

        fints_controller.fetch_fints_transactions(fints_client, ACCOUNT, None, None)

        fints_client.__enter__.assert_called_once_with()
