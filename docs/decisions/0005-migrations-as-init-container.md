# 5. Database migrations run as an ECS init container

**Status:** Accepted

## Context
In AWS, Postgres is RDS in a private subnet. Something must apply `db/migrations` before new code that needs the new schema starts serving. It must happen exactly once per migration, even when several tasks start at the same moment (a deploy, or autoscaling).

## Options considered
1. **A CI step that runs migrations, then deploys.** CI needs network access to a private database (a bastion or a one-off task), and the step must use the *new* image before services are switched to it. It's workable, but it's orchestration glue that has to be kept in sync.
2. **A one-off ECS task per deploy (`aws ecs run-task`), then apply.** The migration task definition must reference the new image *before* the services do, so you end up with a two-phase Terraform apply (`-target`) or task definitions managed outside Terraform.
3. **An init container in every task.** A non-essential `migrate` container runs `python -m app.migrate` from the same image; the app container `dependsOn` it with condition `SUCCESS`. Chosen.

## Decision
Option 3, made safe by the runner itself (`app/migrate.py`):
* A **Postgres advisory lock** serializes concurrent runners: ten tasks starting together apply each migration once (tested with 5 concurrent runners).
* Applied migrations are recorded with a **SHA-256 checksum**; editing one after it ran is a hard error, not a silent skip (tested).
* When the schema is current, the container exits in well under a second.

## Consequences
* Deploy is one `terraform apply -var image_tag=<sha>`: no ordering glue, and no network path from CI to the database.
* If a migration fails, the app container never starts, the task fails its health check, and the ECS circuit breaker rolls back the deployment. Bad schema changes stop themselves.
* **Migrations must be backward compatible** (expand, then contract). During a rolling deploy, old tasks keep running against the new schema until they're replaced. Add a column in one release; stop using the old one in the next; drop it in a third.
* The same runner builds the test database (`tests/conftest.py`), so every CI run also exercises the production migration path.
