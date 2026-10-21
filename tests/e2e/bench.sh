#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
#
# Stands up the end-to-end bench in podman, shared by run.sh: MariaDB, Redis, a fake store serving the
# recorded fixtures, and a bench container (the official ERPNext v16 image + HRMS, the blog app, the
# Webshop fork, the Public Site router and this app) with one site that has every app installed.
# Source it, then call bench_up; bench_down removes everything it created. Every name starts with
# $E2E_PREFIX so concurrent suites never collide. The bench and the fake store both run from one copy of
# the working trees taken when the run starts, so editing the repositories mid-run changes nothing.
set -euo pipefail

E2E_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
E2E_PREFIX="${E2E_PREFIX:-fii-e2e-$$}"
E2E_NETWORK="${E2E_PREFIX}-net"
E2E_DB="${E2E_PREFIX}-db"
E2E_REDIS="${E2E_PREFIX}-redis"
E2E_FAKE="${E2E_PREFIX}-fake"
E2E_BENCH="${E2E_PREFIX}-bench"
E2E_SITES="${E2E_PREFIX}-sites"
E2E_IMAGE="localhost/${E2E_PREFIX}:latest"
E2E_SITE="e2e.test"
E2E_STATE_DIR="${E2E_STATE_DIR:-$(mktemp -d)}"
ERPNEXT_IMAGE="docker.io/frappe/erpnext:v16.36.1"
DB_IMAGE="docker.io/library/mariadb:11.8"
REDIS_IMAGE="docker.io/library/redis:7-alpine"
# version-16 heads of the first-party apps the importer writes into (pinned so a run is reproducible).
HRMS_REF="2478afc7a8f4959d500f6d17522853f0d244e38f"
BLOG_REF="a88363cb5624f8534c1d5c7a543ab329dfa25d80"
PAYMENTS_REF="cca07d9f9392e2ea0e521c5975151db9e4b6c321"
COVERAGE_VERSION="7.10.7"
WEBSHOP_REPO="${WEBSHOP_REPO:-$E2E_ROOT/../frappe-webshop}"
ROUTER_REPO="${ROUTER_REPO:-$E2E_ROOT/../frappe-public-site-router}"
DB_ROOT_PASSWORD="e2e-root"
ADMIN_PASSWORD="e2e-admin"
DB_READY_ATTEMPTS=120
DB_READY_INTERVAL=1
APPS=(erpnext hrms blog payments webshop public_site_router idempotent_importer)
# The fake store's names: two WooCommerce stores, Shopify, and the WooCommerce stores' media host.
FAKE_HOSTS=(woo-a-fake woo-b-fake shopify-fake woo-fake etsy-fake square_online-fake bigcommerce-fake squarespace-fake wix-fake magento-fake lightspeed-fake ecwid-fake odoo-fake dynamics_business_central-fake dynamics_sales-fake dynamics_finance_operations-fake quickbooks_online-fake xero-fake zoho_books-fake zoho_inventory-fake netsuite-fake sap_business_one-fake acumatica-fake sage_intacct-fake square_stored_value-fake agendrix-fake highlevel-fake peoplesoft-fake hubspot-fake)

# The suite's own podman store. Podman's default graph root follows $HOME but its run root and temporary
# directory follow $XDG_RUNTIME_DIR, so two environments with different homes (one per agent session or
# login) share one run root while keeping separate graph roots. Each store then rewrites the run root's
# mount bookkeeping (overlay-layers/mountpoints.json) without the other's containers in it, and an exec
# into a running bench intermittently fails with "unable to find user frappe: no matching entries in passwd
# file" while another environment starts or removes a container. A graph root with a run root of its own
# (keyed by the graph root's path) can't collide with any other store; every podman call below goes
# through it. The base images are pulled into it once and kept. Podman refuses a run root path longer
# than RUN_ROOT_MAX_LENGTH, so the run root's name is short: `fii-` and 12 hex digits of the key.
RUN_ROOT_MAX_LENGTH=50
E2E_STORE="${E2E_STORE:-${XDG_DATA_HOME:-$HOME/.local/share}/frappe-idempotent-importer/e2e-podman}"
E2E_RUN_ROOT="${XDG_RUNTIME_DIR:-/tmp}/fii-$(printf '%s' "$E2E_STORE" | sha256sum | cut -c1-12)"
E2E_PODMAN_RUN_ROOT="$E2E_RUN_ROOT/run"
E2E_STORE_ARGS=(--root "$E2E_STORE/graph" --runroot "$E2E_PODMAN_RUN_ROOT" --tmpdir "$E2E_RUN_ROOT/tmp")
if [ "${#E2E_PODMAN_RUN_ROOT}" -gt "$RUN_ROOT_MAX_LENGTH" ]; then
	echo "The podman run root $E2E_PODMAN_RUN_ROOT is longer than $RUN_ROOT_MAX_LENGTH characters; set a shorter XDG_RUNTIME_DIR." >&2
	exit 1
fi

podman() {
	command podman "${E2E_STORE_ARGS[@]}" "$@"
}

# Copies a repository's working tree (tracked and untracked files, nothing ignored) into a build context.
copy_tree() {
	local repo="$1" target="$2"
	mkdir -p "$target"
	git -C "$repo" ls-files -z --cached --others --exclude-standard \
		| (cd "$repo" && xargs -0 cp --parents --no-dereference -t "$target" 2>/dev/null || true)
}

build_image() {
	local context="$E2E_STATE_DIR/context"
	rm -rf "$context"
	mkdir -p "$context"
	copy_tree "$WEBSHOP_REPO" "$context/webshop"
	copy_tree "$ROUTER_REPO" "$context/public_site_router"
	copy_tree "$E2E_ROOT" "$context/idempotent_importer"
	podman build --quiet --tag "$E2E_IMAGE" \
		--build-arg ERPNEXT_IMAGE="$ERPNEXT_IMAGE" \
		--build-arg HRMS_REF="$HRMS_REF" \
		--build-arg BLOG_REF="$BLOG_REF" \
		--build-arg PAYMENTS_REF="$PAYMENTS_REF" \
		--build-arg COVERAGE_VERSION="$COVERAGE_VERSION" \
		--file "$E2E_ROOT/tests/e2e/Containerfile" "$context" >/dev/null
}

# A bench command inside the bench container, in the bench directory.
bench() {
	podman exec -i -w /home/frappe/frappe-bench "$E2E_BENCH" bench "$@"
}

site() {
	bench --site "$E2E_SITE" "$@"
}

wait_for_db() {
	for _ in $(seq "$DB_READY_ATTEMPTS"); do
		podman exec "$E2E_DB" mariadb-admin ping -h 127.0.0.1 --protocol=tcp -uroot -p"$DB_ROOT_PASSWORD" --silent >/dev/null 2>&1 && return 0
		sleep "$DB_READY_INTERVAL"
	done
	echo "MariaDB never became ready" >&2
	return 1
}

bench_up() {
	build_image
	podman network create "$E2E_NETWORK" >/dev/null
	podman volume create "$E2E_SITES" >/dev/null
	chmod 0777 "$E2E_STATE_DIR"
	podman run -d --name "$E2E_DB" --network "$E2E_NETWORK" --network-alias db \
		-e MARIADB_ROOT_PASSWORD="$DB_ROOT_PASSWORD" "$DB_IMAGE" \
		--character-set-server=utf8mb4 --collation-server=utf8mb4_unicode_ci --skip-character-set-client-handshake >/dev/null
	podman run -d --name "$E2E_REDIS" --network "$E2E_NETWORK" --network-alias redis "$REDIS_IMAGE" >/dev/null
	local aliases=()
	for host in "${FAKE_HOSTS[@]}"; do
		aliases+=(--network-alias "$host")
	done
	podman run -d --name "$E2E_FAKE" --network "$E2E_NETWORK" "${aliases[@]}" \
		-v "$E2E_STATE_DIR/context/idempotent_importer:/app:ro" -v "$E2E_STATE_DIR:/state" -w /app --entrypoint python3 \
		"$E2E_IMAGE" -I tests/e2e/fake_store.py /state >/dev/null
	podman run -d --name "$E2E_BENCH" --network "$E2E_NETWORK" -v "$E2E_SITES:/home/frappe/frappe-bench/sites" \
		-v "$E2E_STATE_DIR:/state" "$E2E_IMAGE" sleep infinity >/dev/null
	printf '%s\n' frappe "${APPS[@]}" | podman exec -i "$E2E_BENCH" tee /home/frappe/frappe-bench/sites/apps.txt >/dev/null
	bench set-config -g db_host db
	bench set-config -gp db_port 3306
	bench set-config -g redis_cache redis://redis:6379
	bench set-config -g redis_queue redis://redis:6379
	bench set-config -g redis_socketio redis://redis:6379
	wait_for_db
	local install=()
	for app in "${APPS[@]}"; do
		install+=(--install-app "$app")
	done
	bench new-site "$E2E_SITE" --db-root-password "$DB_ROOT_PASSWORD" --admin-password "$ADMIN_PASSWORD" \
		--mariadb-user-host-login-scope=% "${install[@]}" >/dev/null
	site execute idempotent_importer.tests.e2e.setup_site
}

bench_down() {
	# --volumes: the images declare anonymous volumes (MariaDB's and Redis's data), which outlive their
	# containers otherwise and pile up in the suite's store run after run.
	podman rm -f --volumes "$E2E_BENCH" "$E2E_FAKE" "$E2E_REDIS" "$E2E_DB" >/dev/null 2>&1 || true
	podman volume rm -f "$E2E_SITES" >/dev/null 2>&1 || true
	podman network rm -f "$E2E_NETWORK" >/dev/null 2>&1 || true
	podman rmi -f "$E2E_IMAGE" >/dev/null 2>&1 || true
}
