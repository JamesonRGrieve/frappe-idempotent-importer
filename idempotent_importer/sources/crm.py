# SPDX-License-Identifier: AGPL-3.0-or-later
"""What the CRM sources (HubSpot, Salesforce, Pipedrive, Zoho CRM) share, so each shape is translated in one
place: the values of the fields a CRM's users defined, which each reader gathers on a node by label."""

from __future__ import annotations

from idempotent_importer.core.node import Node

CUSTOM = "custom"


def custom_of(node: Node) -> dict[str, str]:
	"""The values of the fields the source's users defined, by label (``contract.with_custom``)."""
	custom = node.child(CUSTOM)
	return {label: value for label, value in custom.items() if isinstance(value, str)} if custom else {}
