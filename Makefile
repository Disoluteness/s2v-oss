.PHONY: dev api test lint

dev:
	docker compose up --build

api:
	uvicorn apps.api.app.main:app --reload --host 0.0.0.0 --port 8080

test:
	pytest

lint:
	ruff check .

db-upgrade:
	alembic upgrade head

db-downgrade:
	alembic downgrade -1

db-sql:
	alembic upgrade head --sql
