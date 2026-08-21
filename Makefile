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
