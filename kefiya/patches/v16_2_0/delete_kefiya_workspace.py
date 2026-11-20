# Copyright (c) 2026 Regh & Meier Services GbR and contributors
# SPDX-License-Identifier: AGPL-3.0-only
"""Delete the Kefiya Workspace.

It was never public, so Frappe 16 showed it to no one; the Kefiya Sidebar and
the Kefiya page cover the navigation.
"""

import frappe


def execute():
	if frappe.db.exists("Workspace", {"name": "Kefiya", "module": "Kefiya"}):
		frappe.delete_doc("Workspace", "Kefiya", ignore_missing=True, force=True)
