.PHONY: up down reset logs tenant demo stats test testdb lint fmt dlq bench

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

stats:         ## Sketch-backed stats for today, then the exact answer:  make stats KEY=tk_live_...
	@D=$$(date -u +%F); \
	curl -s "localhost:8000/v1/stats?start=$$D&end=$$D" -H "Authorization: Bearer $(KEY)" | python -m json.tool; \
	echo "--- exact:"; \
	curl -s "localhost:8000/v1/stats?start=$$D&end=$$D&exact=true" -H "Authorization: Bearer $(KEY)" | python -m json.tool

testdb:        ## Create the test database inside the compose Postgres
	docker compose exec postgres psql -U tally -c "CREATE DATABASE tally_test" || true

test:          ## Run all tests (backend needs `make up testdb`)
	cd packages/sketches && python -m pytest -q
	cd services/backend && python -m pytest -q

lint:
	cd packages/sketches && ruff check . && ruff format --check .
	cd services/backend && ruff check . && ruff format --check .

fmt:
	cd services/backend && ruff format . && ruff check --fix .

dlq:           ## How many messages are sitting in the dead-letter queue
	docker compose exec localstack awslocal sqs get-queue-attributes \
	  --queue-url http://localhost:4566/000000000000/tally-events-dlq \
	  --attribute-names ApproximateNumberOfMessages

bench:         ## Re-run sketch accuracy benchmarks (writes packages/sketches/benchmarks/RESULTS.md)
	cd packages/sketches && python benchmarks/accuracy.py
