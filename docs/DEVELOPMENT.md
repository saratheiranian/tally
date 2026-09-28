# Development

## Workflow

- **Trunk-based.** Short-lived branches off `main` (`feat/sqs-sink`, `fix/retry-after-rounding`), merged via PR once CI is green.
- **Conventional Commits** (`feat:`, `fix:`, `test:`, `docs:`, `chore:`, `refactor:`) so the changelog can be generated.
- **Every PR** fills in the template, including what was AI-assisted.
- Releases are tagged `vMAJOR.MINOR.PATCH`.

## AI-assisted development log

I use AI coding tools (Claude, Copilot) for productivity, and treat their output like a junior colleague's PR: useful, and always reviewed. This log records where they helped, and where they were wrong.

| Date | Area | How AI was used | What I verified / changed |
|---|---|---|---|
| _yyyy-mm-dd_ | Rate limiter | _e.g. drafted the Lua token bucket_ | _e.g. wrote the 200-way concurrency test to prove atomicity; switched from client clock to `redis TIME` after reasoning about clock skew_ |
| | | | |

**Rules I follow:**
1. No AI-written code merges without a test I understand that exercises it.
2. SQL gets checked with `EXPLAIN ANALYZE` on realistic data, not trusted by eye.
3. Security-relevant code (auth, key handling) is reviewed line by line.
4. Concurrency tests get **mutation-tested**: temporarily delete the lock or guard they protect and confirm they fail. A race test that passes without its lock proves nothing. (This caught a real case in Phase 3; see ADR 0004.)
5. Benchmarks that disagree with theory get investigated with more trials before anyone trusts or "fixes" them.
