# v5.x.y — <Title>

**Type:** <Algo change | Plumbing | Tooling>
**PDF update required:** <Yes/No>
**ARCHITECTURE.md update:** <Yes/No>
**Risk:** <Low/Medium/High> — <one-sentence reason>

## Decisions needed (resolve BEFORE spawning subagent)
- [ ] Version number (v5.x.y or v5.(x+1).0?)
- [ ] Scope: all tickers / Titans only / specific list?
- [ ] Specific algorithmic decisions...
- [ ] Logging schema: new `[V5xy-<TAG>]` tag name?

## Goals
<one paragraph>

## Scope
<bullet list of components touched>

## Logging schema
<exact new log lines and field names>

## Tests
- Unit: ...
- Integration: ...
- Smoke (post-deploy): expected log lines

## Rollout
- Smoke checks: ...
- Backtest: <which day, which replay script>
- Rollback plan: ...
