# Convenience targets. Docker is the simplest path; the rest are for local dev.
# Local dev uses a SQLite file so Postgres isn't required.

BE = cd backend && . .venv/bin/activate && PYTHONPATH=$(PWD)/backend \
     DATABASE_URL=sqlite+aiosqlite:///./data/app.db \
     SECRET_KEY=$${SECRET_KEY:-dev-secret-change-me-32bytes-minimum} \
     ADMIN_PASSWORD=$${ADMIN_PASSWORD:-dev123}

.PHONY: help up down venv dev test seed-demo seed-sample bot frontend build-frontend

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-16s\033[0m %s\n",$$1,$$2}'

up: ## Run the whole stack with Docker (Postgres + API + bot + SPA)
	docker compose up --build

down: ## Stop the Docker stack
	docker compose down

venv: ## Create the backend venv and install deps (needs python3.12)
	cd backend && python3.12 -m venv .venv && . .venv/bin/activate && pip install -r requirements-dev.txt

dev: ## Run the backend locally on SQLite (http://localhost:8000/docs)
	$(BE) uvicorn app.main:app --reload

test: ## Run backend unit tests
	$(BE) python -m pytest

seed-demo: ## Seed synthetic demo data (no real panel needed)
	$(BE) python -m app.cli.seed_demo

seed-sample: ## Seed from the provided sample backup JSON (if present)
	$(BE) python -m app.cli.seed_sample

bot: ## Run the Telegram bot locally (needs a token in .env / Settings)
	$(BE) python -m app.bot.run

frontend: ## Run the SPA dev server (http://localhost:5173)
	cd frontend && npm install && npm run dev

build-frontend: ## Production build of the SPA
	cd frontend && npm install && npm run build
