# Kefiya on ERPNext 16: implementation plan

Branch: `feat/v16-support`, based on upstream `phamos-eu/kefiya` `version-16`
(v16.1.1). ERPNext 15 is out of scope.

## Where we stand

Tested in the devcontainer (`.devcontainer/`): Frappe 16.60.2, ERPNext 16.60.0,
Payments 16.0.1, Python 3.14, Node 24, MariaDB 11.8.

- `bench get-app`, `install-app` and `migrate` work on a fresh site.
- All server tests pass: 194 (168 + 26, 2 skipped).
- Every kefiya page and form loads without a JavaScript error.

What breaks is outside the tests: reconciliation with ERPNext's own tools,
the scheduled import, links, and the layout of the Kefiya page. Upstream
`version-16` has no fixes for these. Its code is identical to `develop` apart
from the version number and the v16 dependency pins.

## Phase 1: reconciliation (breaks features)

1. **"Create Journal Entry" in the Bank Reconciliation Tool always fails.**
   `kefiya/overrides/bank_reconciliation_tool/bank_reconciliation_tool.py:166-177`
   defines `additional_account_dict` only when `account` is given, but always
   appends it, so it raises `UnboundLocalError`. ERPNext 16's own dialog
   (`erpnext/public/js/bank_reconciliation_tool/dialog_manager.js`) never sends
   `account`, and `hooks.py` sends every call of
   `create_journal_entry_bts` to kefiya.
   Fix: without `account`, call ERPNext's original `create_journal_entry_bts`.
   Build kefiya's four-row Journal Entry only when `account` is given.
   Test: a call without `account` books a two-row entry; a call with it books
   the four-row entry.

2. **Unreconcile in the new Banking page fails** when Kefiya Settings
   "Delete payment entries on unreconciliation" is on. ERPNext 16's
   `unreconcile_transaction` (`bank_transaction.py`) calls
   `remove_payment_entries()` and then cancels the vouchers of type
   "Voucher Created". Kefiya's override
   (`kefiya/overrides/bank_transaction/bank_transaction.py:40-55`) has already
   cancelled and deleted them, so ERPNext's cancel raises `DoesNotExistError`.
   Fix: in the override, leave rows with `reconciliation_type == "Voucher Created"`
   to ERPNext; delete only the rest, and only when the setting is on.
   Test: unreconcile with the setting on and off, for matched and for created
   vouchers.

3. **Vouchers kefiya creates are marked "Matched".** ERPNext 16 records how
   a voucher was linked (`reconciliation_type`) and sets "Voucher Created" when
   `reconcile_vouchers` gets `is_new_voucher=True`. Kefiya does not pass it:
   - `bank_reconciliation_tool.py:219` (the Journal Entry above)
   - `kefiya/utils/client.py` `create_payment_entry`, used by
     `bank_transaction_wiz.js:598-604`

   Effect: ERPNext's unreconcile leaves these vouchers booked.
   Fix: pass `is_new_voucher=True` wherever kefiya creates the voucher.

4. **Scheduled import: date filter.**
   `kefiya/kefiya/doctype/kefiya_schedule/kefiya_schedule.py:55` filters
   `end_date > '1/1/1900'`. Frappe 15 turned that into a date; Frappe 16's query
   builder passes the string to MariaDB as it is.
   Fix: `["is", "set"]`.
   Test: a schedule finds the last import of its login.

## Phase 2: desk (UI)

5. **`/app/...` links.** Frappe 16 desk routes start with `/desk`; `/app` only
   works through a server redirect, which reloads the whole desk.
   - `hooks.py:17` `app_home = "/app/kefiya-hub"`: Frappe 16 accepts only `/desk`
     routes for the apps screen, so with kefiya installed the default path becomes
     `/apps`. Set `/desk/kefiya-hub`.
   - Use `frappe.utils.get_form_link()` / `frappe.set_route()` in
     `kefiya_hub.js:58`, `views/connect.js:98`, `views/imports.js:125`,
     `statement_wizard.js:605`, `kefiya_bank_statement_import.js:88` and
     `kefiya/utils/fints_controller.py:228` (plus the three test assertions in
     `kefiya/tests/test_fints_controller.py`).

6. **`#Form/...` links do nothing.** Frappe 16 dropped hash routes.
   `bank_transaction_wiz.js:893` and `assign_payment_entries.js:336`
   (`get_doc_link`): use `frappe.utils.get_form_link(doctype, name, true)`.

7. **The Kefiya page is too wide.** The `.page-form.row` of `kefiya-hub` ends
   44 px right of the window (1403 px window), which cuts off the last KPI card,
   the step "Match & reconcile" and the buttons on the right of every card.
   The page also shows its own navigation next to Frappe 16's sidebar, which
   lists the same links.
   Fix: drop the `row` margins or the inner navigation in `kefiya_hub.css` /
   `kefiya_hub.js`; keep one navigation, Frappe's sidebar.
   Check: no element wider than the window at 1280 and 1440 px.

8. **Workspace.** `kefiya/kefiya/workspace/kefiya/kefiya.json` has the shape of
   Frappe 13 (`is_standard`, `category`, `pin_to_*`, no `public`), so Frappe 16
   never shows it. The Sidebar (`kefiya/kefiya/sidebar`) and the Dock already
   cover navigation. Delete the workspace (and its record in a patch) or make it
   `public: 1` with a lucide icon.

## Phase 3: clean-up

9. **Drop ERPNext 15:**
   - `hooks.py`: keep only `extend_doctype_class`; remove the version switch.
   - `ci.yml`: remove the `version-15` job.
   - `README.md`: v16 only.
   - `kefiya/tests/ci_setup.py`: remove the `before_tests` fallback.
10. **`kefiya/overrides/user.py`** (`update_password`) has drifted from
    Frappe 16: it lacks the 2FA check for reset keys and the
    `redirect_after_login` clean-up. Copy Frappe 16's version again, keep the one
    intended change (no automatic login), and add a test. Better still: drop the
    override if "log in manually after a reset" is no longer wanted.
11. **Patches:** `patches.txt` lists `v0_0_2.update_kefiya_import` and
    `v0_1_0.replace_kefiya_account_nr`, which do not exist. Migrating a site whose
    Patch Log lacks them fails. Remove the two lines.
12. **SQL for automatic matching:**
    - `kefiya/utils/sql/payment_to_saleInvoice.sql` and
      `payments_to_saleInvoice.sql` use `tabCustomer.advanced_payment_days`,
      which no field defines ("Unknown column").
    - `bank_wizard.sql` groups `tBT.*` by `bank_party_iban`, which fails under
      `ONLY_FULL_GROUP_BY`.

    Fix both, or move them to the query builder.
13. **Dependencies:** declare `sepaxml~=2.7` in `pyproject.toml`; the pain.001
    export imports it, and today it only arrives through `fints`.
    `fints==5.0.0b1` pins `enum-tools~=0.12.0`, which uses `ast.Str` (removed in
    Python 3.14) on one code path in interactive consoles; watch for a fints
    release.

## Order and size

| Step | Items | Size |
| ---- | ----- | ---- |
| 1 | 1-4 reconciliation and schedule, with tests | 1-2 days |
| 2 | 5-8 desk, checked in the browser | 1 day |
| 3 | 9-13 clean-up | 0.5 day |

Each step is one merge request. After each one, run
`bench --site test.localhost run-tests --app kefiya` and click through
reconciliation (Bank Reconciliation Tool, Banking page, Bank Transaction Wizard)
on `kefiya.localhost`.

## Open questions

- Should "Delete payment entries on unreconciliation" stay now that ERPNext 16
  cancels vouchers it created itself?
- Keep kefiya's own Kefiya page navigation or rely on Frappe 16's sidebar?
- Real FinTS cannot be tested in the devcontainer without a bank login. Who
  tests the import against a bank before release?
