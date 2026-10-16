# SPDX-License-Identifier: AGPL-3.0-or-later
from . import __version__ as _version

app_name = "idempotent_importer"
app_title = "Idempotent Importer"
app_publisher = "Zephyrex Technologies Limited"
app_description = (
	"Mirror other stores and business systems into ERPNext, one way and idempotently, until cutover"
)
app_email = "jameson@zephyrex.ca"
app_license = "AGPL-3.0-or-later"
app_version = _version

# Targets: ERPNext (items, parties, sales), Webshop (Website Items, reviews), the blog app (posts) and the
# Public Site router (each connection belongs to one Public Site).
required_apps = ["frappe", "erpnext", "webshop", "blog", "public_site_router"]

scheduler_events = {
	# Every enabled connection: what changed since its last clean run, hourly; everything, daily.
	"hourly_long": ["idempotent_importer.erpnext.jobs.scheduled_incremental"],
	"daily_long": ["idempotent_importer.erpnext.jobs.scheduled_full"],
	# Runs in progress (a pause that is over, a step job that ran out of budget) carry on.
	"cron": {"*/5 * * * *": ["idempotent_importer.erpnext.jobs.resume"]},
}
