# Backend variable audit

This v2 controlled package keeps the existing v1 environment variable names.

Existing variables found in `backend/main.py`:

- `ADMIN_EMAIL`
- `ADMIN_PASSWORD`
- `ANTHROPIC_API_KEY`
- `BASE_URL`
- `DB_PATH`
- `FROM_EMAIL`
- `JWT_SECRET`
- `OPENAI_API_KEY`
- `REPORT_EMAILS`
- `RESEND_API_KEY`

New optional v2 variables added only in `backend/ai/config.py`:

- `OPENAI_REALTIME_MODEL`
- `OPENAI_TEXT_MODEL`
- `CLAUDE_TEXT_MODEL`
- `MEDEX_LEVEL_STRATEGY`

None of these new variables are required for v1 to run.
