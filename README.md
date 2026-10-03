# Delivery Helper

A small web app I use to run a UAT round out of Jira.

Testers each get their own Jira items. This lists them, lets you filter and update them in bulk, ties blocked work to the defect that's blocking it, and drafts the daily emails. The status summary, each tester's to do list and the CSV export all come straight from Jira, so they're never out of date.

A few things it does on purpose:

* Everyone signs in as themselves. Comments, status changes and assignments go to Jira under that person's own API token, so the history shows who actually did it.
* It never sends email. It builds .eml drafts that open in Outlook, and you send them yourself.
* Nothing about one company's Jira is in the code. The project, issue types, epics and status names all live in `config/project.json`.

It's Flask and requests. It runs in Docker with gunicorn, or on Windows with waitress.

## Setting it up

```bash
cp .env.example .env                                  # Jira site, API tokens, cookie key
cp config/project.example.json config/project.json    # your project, epics and statuses
cp config/users.example.json config/users.json        # who can sign in
cp config/testers.example.json config/testers.json    # who runs the tests
docker compose up -d --build
```

Then go to `http://<server>:9090`. No Docker? Run `Run Delivery Helper.bat` on Windows. It checks the same files and uses the same port.

## Config

| File | What's in it |
| --- | --- |
| `.env` | Jira site, one API token per person, the cookie signing key |
| `config/project.json` | project key, issue types, epics, status names, end date, email wording |
| `config/users.json` | who can sign in. It names the token variable, it doesn't hold the token |
| `config/testers.json` | testers, their Jira account ids and email addresses |
| `config/attachments/` | files to attach to every tester email, listed in `project.json` |

None of these get committed. Each one has an `.example` version next to it.

`project.json` gets checked when the app starts. If a key is spelt wrong or a value is the wrong type, it stops and tells you which one. The epics are optional. Leave one blank and whatever uses it just shows nothing.

## What it expects in Jira

* One tester item per person per test case, named `<test name>-<platform>-<TesterName>`. It reads the title from the right, so hyphens in the test name are fine.
* Each tester item linked to the Task or Subtask that holds the test case. The dates and the written test case come from there. If the description has `UAT Ref:`, `Test Case:`, `Pre-Conditions:`, `Functional Steps:` and `Expected Result:` lines, those fill in the export.
* Defects linked with the configured link type and sitting under a defects epic. Only an open defect under one of those epics counts as the reason a test is blocked.

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest
```

86 tests. The web routes run against a fake Jira that records every call, so nothing touches a real one.

## Licence

GPL-3.0. See [LICENSE](LICENSE).
