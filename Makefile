# Santa Claus — install, schedule and clean up.
#
#   make install      link run.sh into $(BIN)
#   make cron         run it every $(INTERVAL) minutes
#   make status       show what is installed
#   make clean        remove the cron entry and the run logs
#
# Override anything on the command line, e.g. `make cron INTERVAL=30`.

REPO     := $(patsubst %/,%,$(dir $(abspath $(lastword $(MAKEFILE_LIST)))))
BIN      ?= $(HOME)/.local/bin
NAME     ?= santaclaus
INTERVAL ?= 10

LINK     := $(BIN)/$(NAME)
LOGS     := $(REPO)/logs
LEDGER   := $(LOGS)/history.csv
HASH     := \#
TAG      := $(HASH) santaclaus
CRON     := */$(INTERVAL) * * * * $(REPO)/run.sh --detach $(TAG)

.PHONY: help install uninstall cron uncron status clean purge guard-tag

# Without this, an empty marker turns every `grep -vF` below into a filter that
# matches every line — i.e. it would erase the whole crontab.
guard-tag:
	@if [ -z "$(strip $(TAG))" ]; then \
		echo "Makefile: TAG is empty; refusing to touch the crontab" >&2; \
		exit 1; \
	fi

help:
	@echo "make install    link $(LINK) -> run.sh"
	@echo "make uninstall  remove the link and the cron entry"
	@echo "make cron       schedule a run every $(INTERVAL) minutes"
	@echo "make uncron     remove the cron entry"
	@echo "make status     show what is installed"
	@echo "make clean      remove the cron entry and the run logs (keeps the ledger)"
	@echo "make purge      also delete $(notdir $(LEDGER)) — needs CONFIRM=yes"

install:
	@mkdir -p "$(BIN)"
	@ln -sfn "$(REPO)/run.sh" "$(LINK)"
	@chmod +x "$(REPO)/run.sh"
	@echo "linked $(LINK) -> $(REPO)/run.sh"
	@case ":$$PATH:" in \
		*":$(BIN):"*) ;; \
		*) echo "note: $(BIN) is not on your PATH" ;; \
	esac

uninstall: uncron
	@rm -f "$(LINK)"
	@echo "removed $(LINK)"

# crontab has no idempotent edit, so rewrite it without our tagged line first.
cron: guard-tag
	@( crontab -l 2>/dev/null | grep -vF "$(TAG)" || true; echo "$(CRON)" ) | crontab -
	@echo "scheduled: $(CRON)"

uncron: guard-tag
	@if crontab -l 2>/dev/null | grep -qF "$(TAG)"; then \
		crontab -l 2>/dev/null | grep -vF "$(TAG)" | crontab -; \
		echo "removed the cron entry"; \
	else \
		echo "no cron entry to remove"; \
	fi

status:
	@if [ -L "$(LINK)" ]; then echo "link:   $(LINK) -> $$(readlink "$(LINK)")"; \
		else echo "link:   not installed"; fi
	@entry=$$(crontab -l 2>/dev/null | grep -F "$(TAG)" || true); \
		if [ -n "$$entry" ]; then echo "cron:   $$entry"; else echo "cron:   not scheduled"; fi
	@if [ -f "$(LEDGER)" ]; then \
		echo "ledger: $$(( $$(wc -l < "$(LEDGER)") - 1 )) attempts in $(LEDGER)"; \
		else echo "ledger: empty"; fi
	@echo "runs:   $$(ls -1 "$(LOGS)"/run-*.log 2>/dev/null | wc -l) kept"

# The ledger is what stops every run from re-attempting the whole roster, which
# is how you get rate-limited. Clean the run logs, keep the ledger.
clean: uncron
	@rm -f "$(LOGS)"/run-*.log "$(LOGS)"/run-*.log.status "$(LOGS)"/run-*.csv
	@rm -f "$(LOGS)/current.log" "$(LOGS)/latest.log" "$(LOGS)/latest.csv"
	@rm -f "$(LOGS)/.run.pid" "$(LOGS)/.run.lock"
	@echo "removed the run logs; kept $(LEDGER)"

purge: clean
	@if [ "$(CONFIRM)" != "yes" ]; then \
		echo "purge deletes $(LEDGER), so the next run re-attempts every"; \
		echo "player and code and will likely be rate-limited."; \
		echo "Re-run as: make purge CONFIRM=yes"; \
		exit 1; \
	fi
	@rm -f "$(LEDGER)" "$(LEDGER)".bad-* "$(LEDGER)".backup-*
	@echo "deleted $(LEDGER)"
