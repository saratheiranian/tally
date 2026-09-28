.PHONY: up down reset logs tenant demo test testdb lint fmt

up:            ## Start the full local stack
	docker compose up -d --build

down:
	docker compose down

reset:         ## Wipe data and re-apply migrations
	docker compose down -v && docker compose up -d --build

logs:
	docker compose logs -f ingest

tenant:        ## Create a tenant + API key:  make tenant NAME=Acme
	docker compose exec ingest python -m app.cli create-tenant --name "$(or $(NAME),Demo)"

demo:          ## Send sample events:  make demo KEY=tk_live_...
	python scripts/send_events.py --key $(KEY)

testdb:        ## Create the test database inside the compose Postgres
	docker compose exec postgres psql -U tally -c "CREATE DATABASE tally_test" || true

test:          ## Run the test suite (needs `make up testdb`)
	cd services/ingest && python -m pytest -q

lint:
	cd services/ingest && ruff check . && ruff format --check .

fmt:
	cd services/ingest && ruff format . && ruff check --fix .
