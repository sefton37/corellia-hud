# corellia-hud: every DoD check runs through a target here (stored checks cannot call interpreters).
SHELL    := /bin/bash
PYTHON   ?= python3
ROOT     := $(CURDIR)
HELM     ?= $(HOME)/dev/helm
EXT_UUID := corellia-hud-pin@corellia.local

.PHONY: test test-classify test-failsoft test-token test-server test-sources \
        test-journal test-chat test-detail test-launcher-policy test-extension \
        mutate-classify check-imports live-crosscheck live-service-check \
        live-detail-check live-unit-check launcher-check launcher-detail-check extension-syntax \
        test-games test-games-state mutate-games e2e-games live-games-check live-games-unit \
        e2e e2e-detail install test-claude-usage test-claude-limits test-claude-state mutate-claude \
        e2e-claude live-claude-check live-claude-unit check-claude-secrets check-claude-secrets-selftest \
        test-services-compact mutate-services e2e-services live-column-check

test: test-classify test-failsoft test-token test-server test-sources \
      test-journal test-chat test-detail test-launcher-policy test-games test-games-state \
      test-claude-usage test-claude-limits test-claude-state test-services-compact

test-classify:
	@$(PYTHON) -m unittest -v tests.test_classify
test-failsoft:
	@$(PYTHON) -m unittest -v tests.test_failsoft
test-token:
	@$(PYTHON) -m unittest -v tests.test_token
test-server:
	@$(PYTHON) -m unittest -v tests.test_server
test-sources:
	@$(PYTHON) -m unittest -v tests.test_sources
test-journal:
	@$(PYTHON) -m unittest -v tests.test_journal
test-chat:
	@$(PYTHON) -m unittest -v tests.test_chat
test-detail:
	@$(PYTHON) -m unittest -v tests.test_detail
test-launcher-policy:
	@$(PYTHON) -m unittest -v tests.test_detail_policy
test-games:
	@$(PYTHON) -m unittest -v tests.test_games
test-games-state:
	@$(PYTHON) -m unittest -v tests.test_games_state

# Claude usage KPI: transcript scanner, the one credentialed HTTPS call, and the /api wiring.
test-claude-usage:
	@$(PYTHON) -m unittest -v tests.test_claude_usage
test-claude-limits:
	@$(PYTHON) -m unittest -v tests.test_claude_limits
test-claude-state:
	@$(PYTHON) -m unittest -v tests.test_claude_state

# Pure window-matching logic and extension.js structure under node (the TAP reporter prints `# pass N`).
test-extension:
	@node --test --test-reporter=tap tests/test_extension.mjs

# Five hand-written mutants of classify.py; the classify suite must kill every one.
mutate-classify:
	@$(PYTHON) tools/mutate_classify.py

check-imports:
	@$(PYTHON) tools/check_imports.py

# Starts the REAL aggregator on a spare loopback port and compares it to live Prometheus.
live-crosscheck:
	@$(PYTHON) tools/live_crosscheck.py

# Probes the installed user unit on 127.0.0.1:8670 and the tailnet address.
live-service-check:
	@$(PYTHON) tools/live_service_check.py

# The INSTALLED unit serves the detail routes (and still refuses the tailnet address).
live-unit-check:
	@$(PYTHON) tools/live_unit_check.py

# Real aggregator on a spare port against the REAL sources: journal, chat tree, read-only proof.
live-detail-check:
	@$(PYTHON) tools/live_detail_check.py

# Seven hand-written mutants of games.py / classify.py; the games suite must kill every one.
mutate-games:
	@$(PYTHON) tools/mutate_games.py

# Real aggregator on a spare port against the REAL game sources (Flint, Bedrock, cgroups, saves).
live-games-check:
	@$(PYTHON) tools/live_games_check.py

# The INSTALLED sandboxed unit on :8670 serves the games key and the game detail kind.
live-games-unit:
	@$(PYTHON) tools/live_games_check.py --unit

# Eleven hand-written mutants of claude_usage.py / claude_limits.py; the three Claude suites must kill every one.
mutate-claude:
	@$(PYTHON) tools/mutate_claude.py

# Source fences around the secret read and the one outbound call: make check-claude-secrets GROUP=secret|network|logging|readonly
check-claude-secrets:
	@$(PYTHON) tools/check_claude_secrets.py $(GROUP)
check-claude-secrets-selftest:
	@$(PYTHON) tools/check_claude_secrets.py --selftest

# Real aggregator on a spare port against the REAL transcripts and the REAL credentials and endpoint.
live-claude-check:
	@$(PYTHON) tools/live_claude_check.py $(ARGS)

# The INSTALLED sandboxed unit on :8670 serves the claude key (proves the sandbox reads the file and reaches the host).
live-claude-unit:
	@$(PYTHON) tools/live_claude_check.py --unit

# Compact Services panel (Spec #492): summary logic, state wiring, the `all` detail id, mutants, browser e2e, live gate.
test-services-compact:
	@$(PYTHON) -m unittest -v tests.test_services_compact

# Nine hand-written mutants of classify.py / detail.py; the compact-services suite must kill every one.
mutate-services:
	@$(PYTHON) tools/mutate_services.py

# Compact Services panel: real aggregator + fakes, 7 scenarios incl. the whole-column height gate at the real window height.
e2e-services:
	@mkdir -p tests/e2e/out
	@NODE_PATH=$(HELM)/node_modules node tests/e2e/services.e2e.cjs

# The REAL installed aggregator on :8670 (read-only GETs): the live column fits the pinned window.
live-column-check:
	@mkdir -p tests/e2e/out
	@NODE_PATH=$(HELM)/node_modules node tests/e2e/live_column.cjs

launcher-check:
	@$(PYTHON) tools/launcher_check.py
	@$(PYTHON) tools/launcher_import.py

# AST check of the detail-window half of the launcher (opens no window).
launcher-detail-check:
	@$(PYTHON) tools/launcher_detail_check.py

# Parse-only check: node tokenises the ES module without resolving gi:// imports.
extension-syntax:
	@$(PYTHON) tools/extension_syntax.py

e2e:
	@mkdir -p tests/e2e/out
	@NODE_PATH=$(HELM)/node_modules node tests/e2e/hud.e2e.cjs

# Detail windows: real aggregator + fake Prometheus / Cloudflare / journalctl + fixture chat tree, 11 scenarios.
e2e-detail:
	@mkdir -p tests/e2e/out
	@NODE_PATH=$(HELM)/node_modules node tests/e2e/detail.e2e.cjs

# Games section: real aggregator + fake Prometheus / Flint / Bedrock responder / journalctl + fixture trees, 8 scenarios.
e2e-games:
	@mkdir -p tests/e2e/out
	@NODE_PATH=$(HELM)/node_modules node tests/e2e/games.e2e.cjs

# Claude panel: the REAL aggregator in-process with a scripted opener + fixture transcript tree, 8 scenarios.
e2e-claude:
	@mkdir -p tests/e2e/out
	@NODE_PATH=$(HELM)/node_modules node tests/e2e/claude.e2e.cjs

# Copies the extension into ~/.local/share/gnome-shell/extensions, links + enables both user
# units, enables the extension in gsettings. Starts ONLY the aggregator: the window unit stays
# stopped until the next login.
install:
	@$(PYTHON) tools/install.py $(EXT_UUID)
