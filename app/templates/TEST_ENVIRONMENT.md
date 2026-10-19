# Template of `.specs/project/TEST-ENVIRONMENT.md`

Direct on purpose: a 2 to 3 line summary and small tables of variable NAMES. No prose sections (what it is for, what may be tested, what never to do: obvious). The app copies the block below to `.specs/project/TEST-ENVIRONMENT.md`; the preflight (`harness.py check`) refuses a file that still has `FILL_IN`, holds a plaintext secret or mentions a `$NAME` no `.env` defines (so keep `$` only on names that exist). In `Where to get it` write only the source, never a value: `appsettings: <file>:<Key:Path>` (key found in appsettings*.json, .env.example or config of the repo), `Infisical: <key> (<path/environment>)` (the code reads it from the secret manager; the AI never opens Infisical or a real `.env`), or `FILL_IN` when the source cannot be determined. Values live in `.specs/project/.env.test`, then `~/.loop-harness/.env`; the Test environment tab lets the user fill the missing ones. `python <shared>/testenv.py list --project <root>` shows which names are defined and from which layer, values masked.

```markdown
# Test environment (DEV)

FILL_IN: 2 to 3 lines: what is tested and against which app or API (DEV and localhost only).

## Where it runs
| Item | Variable | Note |
|------|----------|------|
| Local app | `$LOCAL_URL` | FILL_IN: port and the command that starts it |

## Login (to get the access token)
| User | Password | Token comes from |
|------|----------|------------------|
| `$TEST_USER_1_EMAIL` | `$TEST_USER_1_PASSWORD` | FILL_IN: the login route or page |

## API headers and tokens
| Header or call | Variable | Where to get it |
|----------------|----------|-----------------|
| FILL_IN: header name, for example Authorization | `$ACCESS_TOKEN` | FILL_IN: `appsettings: <file>:<Key:Path>` or `Infisical: <key>` |

## Test data
| What | Variable |
|------|----------|
| FILL_IN: one row per sample the features need (CNPJ, id, address) | `$SAMPLE_ID` |
```
