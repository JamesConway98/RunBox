.DEFAULT_GOAL := help
SHELL := /bin/bash

DATABASE_URL ?= postgresql://runbox:runbox@localhost:5432/runbox
REDIS_URL    ?= redis://localhost:6379/0
export DATABASE_URL
export REDIS_URL

PY := control-plane/.venv/bin

## help: list targets
help:
	@grep -E '^## ' $(MAKEFILE_LIST) | sed 's/^## /  /' | column -t -s ':'

## up: start Postgres and Redis
up:
	docker compose up -d --wait

## down: stop the local stack
down:
	docker compose down

## reset: destroy the local stack and its data
reset:
	docker compose down -v

## db-migrate: apply migrations
db-migrate:
	@$(PY)/python scripts/migrate.py

## db-seed: create demo tenants and print fresh API keys
db-seed:
	@$(PY)/python scripts/seed.py

## setup: install every service's dependencies
setup: setup-api setup-web
	cd runner && go mod download

setup-api:
	cd control-plane && uv venv --python 3.12 .venv && \
		VIRTUAL_ENV=.venv uv pip install -e ".[dev]"

setup-web:
	cd web && npm install

## api: run the control plane with reload
api:
	cd control-plane && .venv/bin/uvicorn runbox_api.main:app --reload --port 8000

## runner: build and run the execution engine
runner:
	cd runner && go run ./cmd/runner

## web: run the dashboard
web:
	cd web && npm run dev

## agent-image: build the sandbox image
agent-image:
	docker build -t runbox/agent:dev ./agent

## test: run every test suite
test: test-api test-runner test-web

test-api:
	cd control-plane && .venv/bin/pytest -q

test-runner:
	cd runner && go test ./...

test-web:
	cd web && npm run test --if-present

## lint: lint every service
lint:
	cd control-plane && .venv/bin/ruff check runbox_api tests
	cd runner && go vet ./...
	cd web && npm run lint

## fmt: format everything
fmt:
	cd control-plane && .venv/bin/ruff format runbox_api tests && .venv/bin/ruff check --fix runbox_api tests
	cd runner && go fmt ./...
	cd web && npm run format --if-present

.PHONY: help up down reset db-migrate db-seed setup setup-api setup-web \
        api runner web agent-image test test-api test-runner test-web lint fmt
