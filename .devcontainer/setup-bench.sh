#!/usr/bin/env bash
# Copyright (c) 2026 Regh & Meier Services GbR and contributors
# SPDX-License-Identifier: AGPL-3.0-only
#
# Set up a Frappe bench with ERPNext and kefiya inside the devcontainer.
# Run once after the container is created; running it again skips what is done.
#
#   bash .devcontainer/setup-bench.sh
#
# Sites: kefiya.localhost for development (setup wizard done) and
# test.localhost for the tests (no setup wizard, see kefiya/tests/ci_setup.py).
set -euo pipefail

step() { printf '\n==> %s\n' "$*"; }

# A site with ERPNext, Payments and kefiya
make_site() {
	local site="$1" app installed
	if [ ! -d "sites/$site" ]; then
		step "new-site $site"
		bench new-site "$site" \
			--db-root-password "$DB_ROOT_PASSWORD" \
			--admin-password "$ADMIN_PASSWORD" \
			--mariadb-user-host-login-scope='%'
	fi
	installed="$(bench --site "$site" list-apps)"
	for app in erpnext payments kefiya; do
		grep -qw "^$app" <<<"$installed" || bench --site "$site" install-app "$app"
	done
}

# A German company with EUR, so the desk opens without the wizard
setup_wizard_args() {
	cat <<-JSON
	{"language": "English", "country": "Germany", "timezone": "Europe/Berlin",
	 "currency": "EUR", "full_name": "Administrator", "email": "admin@example.com",
	 "company_name": "Kefiya Dev GmbH", "company_abbr": "KD", "chart_of_accounts": "Standard",
	 "fy_start_date": "$(date +%Y)-01-01", "fy_end_date": "$(date +%Y)-12-31", "setup_demo": 0}
	JSON
}

# Everything is in main, so bash reads the whole file before it runs it.
main() {
	FRAPPE_BRANCH="${FRAPPE_BRANCH:-version-16}"
	PYTHON_VERSION="${PYTHON_VERSION:-3.14}"
	NODE_VERSION="${NODE_VERSION:-24}"
	BENCH_DIR="${BENCH_DIR:-/workspace/development/frappe-bench}"
	SITE="${SITE:-kefiya.localhost}"
	APP_DIR="${APP_DIR:-/workspace/kefiya}"
	DB_ROOT_PASSWORD="${DB_ROOT_PASSWORD:-123}"
	ADMIN_PASSWORD="${ADMIN_PASSWORD:-admin}"
	TEST_SITE="${TEST_SITE:-test.localhost}"

	step "Node $NODE_VERSION"
	export NVM_DIR="$HOME/.nvm"
	# shellcheck source=/dev/null
	source "$NVM_DIR/nvm.sh"
	nvm install "$NODE_VERSION" >/dev/null
	nvm alias default "$NODE_VERSION" >/dev/null
	nvm use "$NODE_VERSION" >/dev/null
	command -v yarn >/dev/null || npm install -g yarn

	step "Python $PYTHON_VERSION"
	uv python install "$PYTHON_VERSION"
	PYTHON_BIN="$(uv python find "$PYTHON_VERSION")"
	echo "$PYTHON_BIN"

	if [ ! -d "$BENCH_DIR/apps/frappe" ]; then
		step "bench init ($FRAPPE_BRANCH)"
		bench init --skip-redis-config-generation --frappe-branch "$FRAPPE_BRANCH" \
			--python "$PYTHON_BIN" "$BENCH_DIR"
	fi
	cd "$BENCH_DIR"

	step "Point the bench at the compose services"
	bench set-config -g db_host mariadb
	bench set-config -g redis_cache redis://redis-cache:6379
	bench set-config -g redis_queue redis://redis-queue:6379
	bench set-config -g redis_socketio redis://redis-queue:6379
	# Redis runs in its own containers, not from the Procfile
	sed -i '/^redis/d' Procfile

	for app in erpnext payments; do
		if [ ! -d "apps/$app" ]; then
			step "get-app $app ($FRAPPE_BRANCH)"
			bench get-app --skip-assets --branch "$FRAPPE_BRANCH" "$app"
		fi
	done

	if [ ! -e apps/kefiya ]; then
		step "get-app kefiya (linked to $APP_DIR)"
		bench get-app --skip-assets --soft-link "$APP_DIR"
	fi

	make_site "$SITE"
	if [ "$(bench --site "$SITE" execute frappe.is_setup_complete 2>/dev/null | tail -1)" != "true" ]; then
		step "setup wizard on $SITE"
		bench --site "$SITE" execute frappe.desk.page.setup_wizard.setup_wizard.setup_complete \
			--kwargs "{\"args\": $(setup_wizard_args)}"
	fi
	bench --site "$SITE" set-config developer_mode 1
	bench --site "$SITE" enable-scheduler
	bench use "$SITE"

	# Tests need a site the setup wizard has not run on, see kefiya/tests/ci_setup.py
	make_site "$TEST_SITE"
	if ! grep -q '"allow_tests": true' "sites/$TEST_SITE/site_config.json"; then
		bench --site "$TEST_SITE" set-config allow_tests true
		step "test data on $TEST_SITE"
		bench --site "$TEST_SITE" execute kefiya.tests.ci_setup.execute
	fi

	# bench init built frappe already. Building it again runs Frappe Studio's
	# vite build, which needs more than 4 GB of memory, and one app at a time
	# keeps the others small too.
	step "build assets"
	for app in erpnext payments kefiya; do
		bench build --app "$app"
	done

	cat <<-EOF

	Done. Start the bench:
	  cd $BENCH_DIR && bench start
	Open http://localhost:8000, user Administrator, password $ADMIN_PASSWORD.
	Run kefiya's tests:
	  bench --site $TEST_SITE run-tests --app kefiya
	EOF
}

main "$@"
