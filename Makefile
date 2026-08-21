.PHONY: up down logs demo test sdk
up:            ## build and start platform + Postgres on :8080
	docker compose up -d --build
down:
	docker compose down -v
logs:
	docker compose logs -f platform
sdk:           ## install the Python SDK locally (needed by the demo)
	pip install -e sdk/python httpx
demo: sdk      ## run the full loop against localhost:8080 and print console keys
	python demo/run_demo.py
test:          ## run the 8 launch-gate tests (no docker needed)
	python -m pytest tests -q
test-pg:       ## run the gate tests against a throwaway Postgres (needs docker)
	docker run --rm -d --name mantis-test-pg -e POSTGRES_USER=mantis -e POSTGRES_PASSWORD=mantis -e POSTGRES_DB=mantis -p 55432:5432 postgres:16-alpine >/dev/null
	@sleep 4
	-DATABASE_URL=postgresql+psycopg2://mantis:mantis@localhost:55432/mantis python -m pytest tests -q
	docker rm -f mantis-test-pg >/dev/null
