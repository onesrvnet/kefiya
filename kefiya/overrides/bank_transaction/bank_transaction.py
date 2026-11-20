import frappe
from erpnext.accounts.doctype.bank_transaction.bank_transaction import BankTransaction
from erpnext.accounts.doctype.bank_transaction.bank_transaction import (
    unreconcile_transaction as _unreconcile_transaction,
)

class CustomBankTransaction(BankTransaction):
    def before_submit(self):
        super().before_submit()
        self.set_expense_account_and_party()

    def before_update_after_submit(self):
        super().before_update_after_submit()
        self.set_expense_account_and_party()

    def set_expense_account_and_party(self):
        """Derive the Expense Account (and, for Journal Entries, backfill the
        Party) from the voucher(s) this Bank Transaction is reconciled against."""
        if self.unallocated_amount > 0 or not self.payment_entries:
            self.expense_account = None
            return

        company_account = frappe.get_value("Bank Account", self.bank_account, "account")

        self.expense_account = next(
            (
                account
                for pe in self.payment_entries
                if (account := get_voucher_expense_account(pe.payment_document, pe.payment_entry, company_account))
            ),
            None,
        )

        if not self.party:
            for pe in self.payment_entries:
                if pe.payment_document != "Journal Entry":
                    continue
                party_type, party = get_journal_entry_party(pe.payment_entry)
                if party_type and party:
                    self.party_type, self.party = party_type, party
                    break

    @frappe.whitelist()
    def remove_payment_entries(self):
        payment_entries = []
        for payment_entry in self.payment_entries[:]:
            self.remove_payment_entry(payment_entry)
            payment_entries.append(payment_entry)
        # runs on_update_after_submit
        self.save()

        if not delete_entries_on_unreconciling():
            return
        for payment_entry in payment_entries:
            # ERPNext's unreconcile_transaction cancels these itself once this
            # returns, and kefiya's wrapper of it deletes them afterwards
            if frappe.flags.kefiya_in_unreconcile_transaction and is_created_voucher(payment_entry):
                continue
            if payment_entry.payment_document in ("Payment Entry", "Journal Entry"):
                delete_voucher(payment_entry.payment_document, payment_entry.payment_entry)


@frappe.whitelist(methods=["POST"])
def unreconcile_transaction(transaction_name: str | int):
    """ERPNext's unreconcile_transaction (Banking page), with Kefiya Settings'
    "Delete payment entries on unreconciliation".

    ERPNext cancels the vouchers that were created to reconcile the
    transaction. With the setting on, kefiya deletes them afterwards, and
    deletes the matched ones in remove_payment_entries.
    """
    created = [
        (row.payment_document, row.payment_entry)
        for row in frappe.get_doc("Bank Transaction", transaction_name).payment_entries
        if is_created_voucher(row)
    ]

    frappe.flags.kefiya_in_unreconcile_transaction = True
    try:
        result = _unreconcile_transaction(transaction_name)
    finally:
        frappe.flags.kefiya_in_unreconcile_transaction = False

    if delete_entries_on_unreconciling():
        for doctype, name in created:
            if doctype in ("Payment Entry", "Journal Entry"):
                delete_voucher(doctype, name)
    return result


def is_created_voucher(payment_entry):
    return payment_entry.get("reconciliation_type") == "Voucher Created"


def delete_entries_on_unreconciling():
    return frappe.db.get_single_value("Kefiya Settings", "delete_payment_entries_on_unreconciliation")


def delete_voucher(doctype, name):
    """Cancel the voucher if it is still submitted, then delete it.

    ERPNext deletes a voucher with ledger entries only when Accounts Settings
    allows it ("Delete Accounting and Stock Ledger Entries on deletion of
    Transaction"); otherwise the voucher stays, cancelled.
    """
    voucher = frappe.get_doc(doctype, name)
    if voucher.docstatus == 1:
        voucher.cancel()
    if frappe.db.get_single_value("Accounts Settings", "delete_linked_ledger_entries"):
        voucher.delete()


def get_voucher_expense_account(payment_document, payment_entry, company_account):
    """Best-effort guess of the Expense/Income Account booked against a
    reconciled voucher, excluding the company's own bank account."""
    if payment_document == "Payment Entry":
        # A Bank Transaction can be reconciled against a Payment Entry that was
        # itself created to settle one or more Purchase Invoices (e.g. the
        # Mastercard matching flow) - follow that reference to the real invoice.
        purchase_invoices = frappe.get_all(
            "Payment Entry Reference",
            filters={"parent": payment_entry, "reference_doctype": "Purchase Invoice"},
            pluck="reference_name",
        )
        return next(
            (
                account
                for pi in purchase_invoices
                if (account := get_voucher_expense_account("Purchase Invoice", pi, company_account))
            ),
            None,
        )

    if payment_document == "Purchase Invoice":
        accounts = frappe.get_all(
            "Purchase Invoice Item", filters={"parent": payment_entry}, pluck="expense_account"
        )
    elif payment_document == "Journal Entry":
        accounts = frappe.get_all(
            "Journal Entry Account",
            filters={"parent": payment_entry, "party_type": ["in", ["", None]]},
            pluck="account",
        )
    else:
        return None

    accounts = sorted({account for account in accounts if account and account != company_account})
    return accounts[0] if accounts else None


def get_journal_entry_party(journal_entry):
    row = frappe.get_all(
        "Journal Entry Account",
        filters={"parent": journal_entry, "party_type": ["is", "set"], "party": ["is", "set"]},
        fields=["party_type", "party"],
        limit=1,
    )
    return (row[0].party_type, row[0].party) if row else (None, None)