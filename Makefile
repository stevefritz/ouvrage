.PHONY: help install test test-quick run docker docker-dev docker-build clean

help:
	@echo "Ouvrage — common tasks"
	@echo ""
	@echo "  make install      Install the package with dev extras into the current environment"
	@echo "  make test         Run the full test suite"
	@echo "  make test-quick   Run only tests that failed on the last run (pytest --last-failed)"
	@echo "  make run          Start the server locally (python -m ouvrage)"
	@echo "  make docker       Start the production container (docker compose up -d)"
	@echo "  make docker-dev   Start the dev container with code mounted live"
	@echo "  make docker-build Rebuild the production image"
	@echo "  make clean        Remove caches and build artifacts"

install:
	pip install -e '.[dev]'

test:
	python -m pytest tests/ -q --tb=short

test-quick:
	python -m pytest tests/ --last-failed --tb=short -v

run:
	python -m ouvrage

docker:
	docker compose -f docker-compose.example.yml up -d

docker-dev:
	docker compose -f docker-compose.example.yml -f docker-compose.dev.yml up

docker-build:
	docker compose -f docker-compose.example.yml build

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	rm -rf build dist *.egg-info
