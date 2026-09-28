.PHONY: up down reset logs tenant demo test testdb lint fmt dlq

up:            ## Start the full local stack
	docker compose up -d --build

down:
	docker compose down

reset:         ## Wipe data and re-apply migrations
	docker compose down -v && docker compose up -d --build

logs:
	docker compose logs -f ingest worker-a worker-b

tenant:        ## Create a tenant + API key:  make tenant NAME=Acme
	docker compose exec ingest python -m app.cli create-tenant --name "$(or $(NAME),Demo)"

demo:          ## Send sample events:  make demo KEY=tk_live_...
	python scripts/send_events.py --key $(KEY)

testdb:        ## Create the test database inside the compose Postgres
	docker compose exec postgres psql -U tally -c "CREATE DATABASE tally_test" || true

test:          ## Run the test suite (needs `make up testdb`)
	cd services/backend && python -m pytest -q

lint:
	cd services/backend && ruff check . && ruff format --check .

fmt:
	cd services/backend && ruff format . && ruff check --fix .

dlq:           ## How many messages are sitting in the dead-letter queue
	docker compose exec localstack awslocal sqs get-queue-attributes \
	  --queue-url http://localhost:4566/000000000000/tally-events-dlq \
	  --attribute-names ApproximateNumberOfMessages
