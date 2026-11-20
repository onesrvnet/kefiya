# Copyright (c) 2026 Regh & Meier Services GbR and contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Reconciling and unreconciling with ERPNext 16's tools while kefiya is installed.

Kefiya replaces create_journal_entry_bts and unreconcile_transaction and
extends Bank Transaction.remove_payment_entries; ERPNext's Bank
Reconciliation Tool, its Banking page and the Bank Transaction form call them.
"""

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import nowdate

from kefiya.overrides.bank_reconciliation_tool.bank_reconciliation_tool import (
    custom_create_journal_entry_bts,
)
from kefiya.overrides.bank_transaction.bank_transaction import unreconcile_transaction
from kefiya.tests.ci_setup import BANK_ACCOUNT, OTHER_COMPANY


def leaf_account(root_type, exclude=None):
    return frappe.db.get_value(
        "Account",
        {
            "company": OTHER_COMPANY,
            "root_type": root_type,
            "is_group": 0,
            "account_type": ["in", ["", None]],
            "name": ["!=", exclude or ""],
        },
        order_by="name asc",
    )


class TestReconciliation(FrappeTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.bank_account = frappe.db.get_value("Bank Account", {"account_name": BANK_ACCOUNT})
        cls.income_account = leaf_account("Income")
        cls.other_account = leaf_account("Income", exclude=cls.income_account)

    def setUp(self):
        self.set_delete_setting(0)

    def set_delete_setting(self, value, ledger=1):
        """Kefiya's setting, and ERPNext's, without which no voucher with
        ledger entries can be deleted."""
        frappe.db.set_single_value(
            "Kefiya Settings", "delete_payment_entries_on_unreconciliation", value
        )
        frappe.db.set_single_value("Accounts Settings", "delete_linked_ledger_entries", ledger)

    def make_transaction(self):
        return frappe.get_doc({
            "doctype": "Bank Transaction",
            "date": nowdate(),
            "bank_account": self.bank_account,
            "company": OTHER_COMPANY,
            "currency": "EUR",
            "deposit": 100,
            "description": "_Test Kefiya reconciliation",
        }).insert().submit()

    def create_journal_entry(self, transaction, **kwargs):
        custom_create_journal_entry_bts(
            transaction.name,
            reference_number="KEF-TEST",
            reference_date=nowdate(),
            posting_date=nowdate(),
            entry_type="Bank Entry",
            second_account=self.income_account,
            **kwargs,
        )
        rows = frappe.get_doc("Bank Transaction", transaction.name).payment_entries
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_erpnext_dialog_books_two_rows(self):
        """ERPNext's dialog sends no account; kefiya used to fail with UnboundLocalError."""
        row = self.create_journal_entry(self.make_transaction())

        self.assertEqual(row.reconciliation_type, "Voucher Created")
        journal_entry = frappe.get_doc("Journal Entry", row.payment_entry)
        self.assertEqual(journal_entry.docstatus, 1)
        self.assertEqual(len(journal_entry.accounts), 2)

    def test_wizard_books_four_rows_as_created_voucher(self):
        row = self.create_journal_entry(self.make_transaction(), account=self.other_account)

        self.assertEqual(row.reconciliation_type, "Voucher Created")
        journal_entry = frappe.get_doc("Journal Entry", row.payment_entry)
        self.assertEqual(len(journal_entry.accounts), 4)
        self.assertEqual(journal_entry.total_debit, 100)

    def test_banking_unreconcile_cancels_the_created_entry(self):
        transaction = self.make_transaction()
        row = self.create_journal_entry(transaction)

        unreconcile_transaction(transaction.name)

        self.assertFalse(frappe.get_doc("Bank Transaction", transaction.name).payment_entries)
        self.assertEqual(frappe.db.get_value("Journal Entry", row.payment_entry, "docstatus"), 2)

    def test_banking_unreconcile_deletes_the_created_entry_with_the_setting(self):
        """ERPNext cancels the entry after remove_payment_entries; kefiya used to
        delete it first, so ERPNext's cancel failed with DoesNotExistError."""
        self.set_delete_setting(1)
        transaction = self.make_transaction()
        row = self.create_journal_entry(transaction)

        unreconcile_transaction(transaction.name)

        self.assertFalse(frappe.get_doc("Bank Transaction", transaction.name).payment_entries)
        self.assertFalse(frappe.db.exists("Journal Entry", row.payment_entry))

    def test_form_unreconcile_deletes_the_entry_with_the_setting(self):
        self.set_delete_setting(1)
        transaction = self.make_transaction()
        row = self.create_journal_entry(transaction)

        frappe.get_doc("Bank Transaction", transaction.name).remove_payment_entries()

        self.assertFalse(frappe.db.exists("Journal Entry", row.payment_entry))

    def test_entry_is_only_cancelled_when_erpnext_keeps_ledger_entries(self):
        self.set_delete_setting(1, ledger=0)
        transaction = self.make_transaction()
        row = self.create_journal_entry(transaction)

        unreconcile_transaction(transaction.name)

        self.assertEqual(frappe.db.get_value("Journal Entry", row.payment_entry, "docstatus"), 2)

    def test_form_unreconcile_keeps_the_entry_without_the_setting(self):
        transaction = self.make_transaction()
        row = self.create_journal_entry(transaction)

        frappe.get_doc("Bank Transaction", transaction.name).remove_payment_entries()

        self.assertEqual(frappe.db.get_value("Journal Entry", row.payment_entry, "docstatus"), 1)
