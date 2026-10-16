# Template of `.specs/project/TEST-ENVIRONMENT.md` (the project's test environment)

A **project** file, never a skill file. Copy this template to `.specs/project/TEST-ENVIRONMENT.md` and replace every `FILL_IN`. The preflight (`harness.py check`, the **Check** button in the panel) refuses to start when the ROADMAP has `regression=yes` (or the loop runs with `--e2e`) and this file is missing or still has `FILL_IN`.

**Why it exists:** validating a route runs the real app and needs headers, users and keys. Without them the loop stops at the baseline and wakes you up for something that was already known when the routes were read. Whoever writes the specs finds this out in Phase 2 and closes it **before** asking for the roadmap approval.

**Secret:** the file may hold DEV values. Check that `.specs/` is in the project's `.gitignore` (`git check-ignore -v .specs/project/TEST-ENVIRONMENT.md`). Never copy a value into evidence, summary, notification or commit (write `***`). Never production.

**Key in your secret manager:** every credential states **the exact key in your secret manager**, the environment and the class that reads it. You need this to know where to take the value from. The project's keys: `CLAUDE.md` (a "Keys in use" section, if the project has one) and the configuration lookups in the code.

```markdown
# Test environment (DEV): variables, headers and credentials

> SECRET. Only DEV and localhost. .specs/ is ignored by git. Never copy values into a log, evidence, summary, notification or commit.
> User authorization (FILL_IN date): the accesses below may be used to validate the routes.

## 1. Where it runs
| What | Value |
|---|---|
| Local app | FILL_IN (https://localhost:PORT, then http://localhost:PORT) |
| Route prefix | FILL_IN (/api/v1) |
| SSO / login | FILL_IN (DEV identity provider URL and login route) |
| Database | FILL_IN (DEV; never production) |

## 2. Credentials and headers
| For what | Header or call | Value | Key in your secret manager (environment, class that reads it) |
|---|---|---|---|
| Admin routes | FILL_IN (e.g. X-Admin-Token) | FILL_IN | FILL_IN (e.g. Authentication:AdminToken, dev, AdminTokenAuthenticationHandler) |
| Customer routes | FILL_IN (e.g. Authorization: Bearer) | FILL_IN (how to obtain it) | FILL_IN |
| Webhook / integration | FILL_IN | FILL_IN | FILL_IN |

## 3. Test users
| Role | E-mail | Password | For what |
|---|---|---|---|
| FILL_IN | FILL_IN | FILL_IN | FILL_IN |

## 3b. User or account with test data (happy path)
| User or account | Has which data | For which cases |
|---|---|---|
| FILL_IN | FILL_IN (e.g. 12 orders and 3 refunds) | FILL_IN (happy-path RG-xx cases) |
Checked on FILL_IN (date), by reading the DEV database or asking the user. An account without data only covers edge cases (401, 403, invalid body, empty list). If the service sends e-mail or charges a third party when a record changes, write here how to avoid it.

## 4. What each spec uses
| Spec | RG cases | Credential |
|---|---|---|
| FILL_IN | FILL_IN | FILL_IN |

## 5. Allowed and forbidden
Allowed (DEV): FILL_IN.
Forbidden without exception: FILL_IN (e.g. creating a resource billed by a third party; anything in production).

## 6. Pending items
- FILL_IN (value still missing, and who provides it). Nothing pending: write "none".
```
