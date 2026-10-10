# Documentation

The repository README is the quick start. These guides describe current
operation and implementation; `research/` contains dated assessments and a
historical model baseline. Run commands from the repository root unless a guide
says otherwise. Configuration defaults are defined in `.env.example` and
`engine/lisa/config.py`; runtime overrides can change effective settings.

## Operations

- [Local server and workers](operations/LOCAL_RUNNING.md)
- [Vercel and Supabase production testing](operations/VERCEL.md)
- [Isolated Docker/PostgreSQL paper pilot](operations/PAPER_PILOT.md)
- [Checks and deployment acceptance](operations/CHECKS.md)
- [Manual paper grading and tier verification](operations/MANUAL_VERIFICATION.md)

## Architecture and product

- [System and hosting boundaries](architecture/SYSTEM.md)
- [Database, migrations and backups](architecture/DATABASE.md)
- [Generation, publication and settlement](architecture/DAILY_SERVICES.md)
- [Models and evaluation](architecture/MODELS.md)
- [Frontend/backend API contract](architecture/FRONTEND_API.md)
- [Pick selection, ordering and subscription access](product/PICK_FEED.md)

## Data supply

- [Provider roles and routing](providers/DATA_SOURCES.md)
- [Quota-aware coverage](providers/COVERAGE.md)
- [Bookmaker price adapters and credentials](providers/ODDS.md)
- [Authenticated provider validation](providers/VALIDATION.md)
- [Scalper setup, collectors and interchange](scalper/README.md)
- [Scalper persistence and failure handling](scalper/DESIGN.md)
- [Scalper coverage and remaining work](scalper/COVERAGE.md)

## Frontend

- [Development and browser checks](frontend/DEVELOPMENT.md)
- [Design system](frontend/DESIGN.md)

## Research

- [Free-source assessment](research/FREE_SOURCES.md)
- [File-source assessment](research/FILE_SOURCES.md)
- [Historical model evaluation](research/model-evaluation.json)

Research observations describe their inspection dates, not current provider
entitlements. Regenerate live checks before relying on access or pricing.
Historical model metrics do not authorize staking.

New generated reports go to ignored `data/reports/`, not this directory. Previous
machine-local validation reports are preserved locally in
`data/reports/legacy/`; Git history retains removed drafts and release journals.
Keep durable documentation in the relevant topic directory and link it here.
