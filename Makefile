SHELL := /bin/bash

.PHONY: check-runtime setup dev test lint typecheck eval-deterministic reset

check-runtime:
	@bash scripts/check_runtime.sh

setup:
	@bash scripts/setup.sh

dev:
	@bash scripts/dev.sh

test:
	@bash scripts/verify.sh test

lint:
	@bash scripts/verify.sh lint

typecheck:
	@bash scripts/verify.sh typecheck

eval-deterministic:
	@bash scripts/eval_deterministic.sh

reset:
	@bash scripts/reset.sh
