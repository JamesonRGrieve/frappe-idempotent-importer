#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# End-to-end suite on a real bench (tests/e2e/bench.sh): ERPNext v16 + HRMS + the blog app + the Webshop
# fork + the Public Site router + this app, importing from a fake store that serves the recorded fixtures
# for two WooCommerce connections (each bound to its own Public Site and company) and a Shopify
# connection. It proves the first import, that unchanged full and incremental re-runs write nothing
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

run_all() {
	for connection in woo-a woo-b shop-s; do
		step run "$connection" "$1"
	done
}

# The suite is one function, called on the last line: bash reads a script as it runs it, so editing run.sh
# mid-run would otherwise change (or break) the rest of the run.
suite() {
	for host in "${FAKE_HOSTS[@]}"; do
		echo v1 > "$E2E_STATE_DIR/$host-state"
	done
	echo "== Standing up the bench (image build, site with every app, three businesses)"
	bench_up
	podman exec -d -w /home/frappe/frappe-bench "$E2E_BENCH" bench worker --queue long

	echo "== First import of every connection"
	run_all full
	step assert_woocommerce woo-a v1
	step assert_woocommerce woo-b v1
	step assert_shopify v1
	step assert_shared_coupon_codes welcome10 woo-a woo-b
	step assert_storeless_site_refuses_codes woo-a

	echo "== Unchanged sources: full and incremental re-runs must write nothing"
	step checksum v1
	run_all full
	run_all incremental
	step assert_unchanged v1

	store_state woo-a-fake v2
	store_state shopify-fake v2
	echo "== Changed: a price, stock, a deleted product, a renamed category, a refund, a payment, new orders,"
	echo "   an edited and a deleted page, a new post and review, a deleted coupon and redirect (woo-a);"
	echo "   a deleted product and a new order (shop-s). woo-b is unchanged."
	step run woo-a full
	step run shop-s full
	step assert_woocommerce woo-a v2
	step assert_shopify v2
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
	echo "E2E PASSED"
}

suite; exit
