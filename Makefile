.PHONY: build install run test-token clean help

BINARY := bin/zerodha_autologin
PYTHON  := python3

GO := $(shell which go 2>/dev/null \
        || ls /usr/local/go/bin/go 2>/dev/null \
        || ls /usr/bin/go 2>/dev/null \
        || ls /snap/bin/go 2>/dev/null)

help:
	@echo "Nifty Monthly Iron Fly — Makefile targets"
	@echo ""
	@echo "  make build       Build the Go autologin binary"
	@echo "  make install     Full setup: build + pip install + systemd + cron"
	@echo "  make run         Start the trading bot (DRY_RUN=true by default)"
	@echo "  make test-token  Run autologin once and verify token is written"
	@echo "  make clean       Remove compiled binary"

build:
	@if [ -z "$(GO)" ]; then \
		echo "ERROR: Go not found. Install with:"; \
		echo "  sudo apt install golang-go"; \
		echo "  # or: sudo snap install go --classic"; \
		exit 1; \
	fi
	@echo "==> Building autologin binary (using $(GO))..."
	@mkdir -p bin
	$(GO) build -o $(BINARY) ./cmd/zerodha_autologin/
	@chmod +x $(BINARY)
	@echo "    Built: $(BINARY)"

install: build
	@bash deploy/install.sh

run:
	$(PYTHON) main.py

test-token: build
	@echo "==> Running autologin..."
	@bash scripts/zerodha_autologin.sh
	@echo ""
	@echo "==> Token (first 10 chars):"
	@head -c 10 secrets/kite_access_token && echo "..."
	@echo ""
	@echo "OK — token file is ready."

clean:
	rm -f $(BINARY)
