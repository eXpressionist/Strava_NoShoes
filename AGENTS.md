# Repository Guidelines

## Project Structure & Module Organization

This FastAPI app manages Strava activities and GPX downloads. Core source lives in `app/`:

- `app/main.py` wires the FastAPI app, templates, static files, and health checks.
- `app/api/` defines API routes.
- `app/models/` contains Pydantic Strava models.
- `app/services/` contains Strava, bot, and business logic.
- `app/utils/` contains auth, file, and GPX helpers.
- `app/templates/` and `app/static/` contain web UI files.

Tests live in `tests/`. Runtime state belongs in `data/` and `logs/`; do not commit generated files from those directories.

## Build, Test, and Development Commands

- `poetry install` installs application and development dependencies.
- `pip install -r requirements.txt` is the non-Poetry fallback.
- `poetry run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000` runs the dev server.
- `python run.py` starts the application through the project runner.
- `poetry run pytest` runs the test suite.
- `docker-compose up --build` builds and runs the containerized app on port `8000`.

Copy `.env.example` to `.env` before local runs that need Strava credentials.

## Coding Style & Naming Conventions

Use Python 3.11+ syntax compatible with the configured tooling. Format with Black at 88 columns and sort imports with isort's Black profile:

```bash
poetry run black app tests
poetry run isort app tests
poetry run flake8 app tests
poetry run mypy app
```

Use `snake_case` for modules, functions, variables, and tests. Use `PascalCase` for Pydantic models and service classes. Keep async I/O paths async.

## Testing Guidelines

The suite uses `pytest`, `pytest-asyncio`, and FastAPI `TestClient`. Add tests under `tests/` as `test_<feature>.py` with functions named `test_<behavior>`. Prefer focused endpoint, model, and service tests. Mock Strava network calls instead of requiring live credentials.

Run `poetry run pytest` before submitting changes.

## Commit & Pull Request Guidelines

History is minimal and uses short imperative messages such as `Update docker-compose.yml`. Keep commits concise, present tense, and scoped to one change.

Pull requests should include a brief summary, testing performed, configuration changes, and screenshots for UI changes. Link related issues when available. Do not include secrets, `.env`, downloaded GPX files, or generated logs.

## Security & Configuration Tips

Store Strava tokens and application secrets only in `.env` or the deployment environment. Treat `data/` and `logs/` as local runtime state. When changing Docker or deployment settings, update `DOCKER_DEPLOYMENT.md` if operator steps change.
