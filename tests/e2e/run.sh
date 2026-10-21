#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# End-to-end suite on a real bench (tests/e2e/bench.sh): ERPNext v16 + HRMS + the blog app + the Webshop
# fork + the Public Site router + this app, importing from a fake store that serves the recorded fixtures
# for two WooCommerce connections and one connection to every other built source, each bound to its own
# Public Site and company. It proves the first import, that unchanged full and incremental re-runs write nothing
# (every content table byte-identical), that changes and deletions land, that one connection's runs never
# touch another's records, that a rate-limited source pauses the run in place and rejected credentials
# hold scheduled runs, that Sync Now runs on the background queue, and that nothing sends mail.
# Everything it creates is removed on exit (E2E_KEEP=1 keeps it for inspection).
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/bench.sh"

cleanup() {
	local status=$?
	if [ "$status" -ne 0 ]; then
		echo "== Last run logs"
		podman exec -i -w /home/frappe/frappe-bench "$E2E_BENCH" bench --site "$E2E_SITE" mariadb \
			< "$E2E_ROOT/tests/e2e/last-run-logs.sql" 2>/dev/null || true
	fi
	if [ -z "${E2E_KEEP:-}" ]; then
		bench_down
		rm -rf "$E2E_STATE_DIR"
	fi
}
trap cleanup EXIT
trap 'exit 130' INT TERM

# One check of idempotent_importer.tests.e2e: its name and arguments, as words.
step() {
	echo "-- $*"
	printf '%s\n' "$@" > "$E2E_STATE_DIR/e2e-step"
	site execute idempotent_importer.tests.e2e.main
}

store_state() {
	echo "$2" > "$E2E_STATE_DIR/$1-state"
	echo "== $1 serves fixture state $2"
}

# Every source besides WooCommerce, one connection each: <connection>:<fake host>:<its check>. Each serves
# its own v1 fixtures, then v2 (its changes and deletions).
SOURCES=(
	shop-s:shopify-fake:assert_shopify
	etsy-e:etsy-fake:assert_etsy
	square-q:square_online-fake:assert_square_online
	bigcommerce-c:bigcommerce-fake:assert_bigcommerce
	squarespace-p:squarespace-fake:assert_squarespace
	wix-x:wix-fake:assert_wix
	magento-m:magento-fake:assert_magento
	lightspeed-l:lightspeed-fake:assert_lightspeed
	ecwid-c:ecwid-fake:assert_ecwid
	odoo-o:odoo-fake:assert_odoo
	business-central-b:dynamics_business_central-fake:assert_business_central
	dynamics-sales-d:dynamics_sales-fake:assert_dynamics_sales
	finance-operations-f:dynamics_finance_operations-fake:assert_finance_operations
	quickbooks-q:quickbooks_online-fake:assert_quickbooks_online
	xero-x:xero-fake:assert_xero
	zoho-books-b:zoho_books-fake:assert_zoho_books
	zoho-inventory-i:zoho_inventory-fake:assert_zoho_inventory
	netsuite-n:netsuite-fake:assert_netsuite
	sap-b:sap_business_one-fake:assert_sap_business_one
	acumatica-a:acumatica-fake:assert_acumatica
	intacct-s:sage_intacct-fake:assert_sage_intacct
)
# The connections approved through OAuth 2: Connect is proven on each.
OAUTH_CONNECTIONS=(etsy-e quickbooks-q xero-x zoho-books-b acumatica-a)
CONNECTIONS=(woo-a woo-b)
for source in "${SOURCES[@]}"; do
	CONNECTIONS+=("${source%%:*}")
done

run_all() {
	for connection in "${CONNECTIONS[@]}"; do
		step run "$connection" "$1"
	done
}

# Each source's check against fixture state $1.
assert_sources() {
	for source in "${SOURCES[@]}"; do
		step "${source##*:}" "$1"
	done
}

# The suite is one function, called on the last line: bash reads a script as it runs it, so editing run.sh
# mid-run would otherwise change (or break) the rest of the run.
suite() {
	for host in "${FAKE_HOSTS[@]}"; do
		echo v1 > "$E2E_STATE_DIR/$host-state"
	done
	echo "== Standing up the bench (image build, site with every app, a business per connection)"
	bench_up
	podman exec -d -w /home/frappe/frappe-bench "$E2E_BENCH" bench worker --queue long

	echo "== First import of every connection"
	run_all full
	step assert_woocommerce woo-a v1
	step assert_woocommerce woo-b v1
	assert_sources v1
	for connection in "${OAUTH_CONNECTIONS[@]}"; do
		step assert_oauth_connect "$connection"
	done
	step assert_shared_coupon_codes welcome10 woo-a woo-b

	echo "== Unchanged sources: full and incremental re-runs must write nothing"
	step checksum v1
	run_all full
	run_all incremental
	step assert_unchanged v1

	store_state woo-a-fake v2
	for source in "${SOURCES[@]}"; do
		host="${source#*:}"
		store_state "${host%%:*}" v2
	done
	echo "== Changed: a price, stock, a deleted product, a renamed category, a refund, a payment, new orders,"
	echo "   an edited and a deleted page, a new post and review, a deleted coupon and redirect (woo-a);"
	echo "   each other source's own changes and deletions. woo-b is unchanged."
	step run woo-a full
	for source in "${SOURCES[@]}"; do
		step run "${source%%:*}" full
	done
	step assert_woocommerce woo-a v2
	assert_sources v2
	step assert_woocommerce woo-b v1
	step assert_connection_untouched v1 woo-b

	echo "== Changed sources re-run: nothing more to write"
	step checksum v2
	run_all full
	run_all incremental
	step assert_unchanged v2

	echo "== Lockout safety"
	step assert_lockout_safety woo-a
	step assert_unchanged v2

	echo "== Sync Now, on the background job queue"
	step sync_now_in_background woo-b
	step assert_unchanged v2

	echo "== A listed (not built) source refuses to run"
	step assert_listed_sources_refuse woo-a
	step assert_unchanged v2

	step assert_no_side_effects

	echo "== The writers' unit tests (bench run-tests, each test rolled back) and their coverage ratchet"
	site set-config allow_tests true
	site run-tests --app idempotent_importer --module idempotent_importer.tests.test_writers \
		--coverage --skip-before-tests
	podman exec -w /home/frappe/frappe-bench/apps/idempotent_importer "$E2E_BENCH" \
		../../env/bin/python -m tools.writer_coverage /home/frappe/frappe-bench/sites/coverage.xml
	echo "E2E PASSED"
}

suite; exit
