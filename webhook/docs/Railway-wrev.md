# wrev on Railway

Railway follows `r33drichards/webhook` on `master` and builds the Dockerfile
after each push. The Dockerfile pins the native reviewer to a merged Wrev SHA.
Leave the Railway `WREV_REVISION` variable unset so the committed pin controls
the build. The Wrev Actions workflow proposes pin updates as worker PRs.
Merge the pin PR to promote that revision to production.

Keep one replica, the existing volume at `/data`, and the `/` health check.

| Railway variable | Value |
| --- | --- |
| `WREV_DATA_DIR` | `/data` |
| `WREV_REPOSITORY_OWNER` | GitHub user or organization login |
| `WREV_LABEL` | Optional, defaults to `wrev` |
| `WREV_WEBHOOK_SECRET` | Random signing secret shared with GitHub |
| `WREV_CREDENTIAL_ADMIN_TOKEN` | Separate random bearer token, minimum 32 characters |
| `WREV_CREDENTIAL_ENCRYPTION_KEY` | Separate key of 32 random bytes encoded as 64 hex characters |
| `WREV_GITHUB_TOKEN` | GitHub token with read access to Contents and Pull requests |
| `WREV_GITHUB_COMMENT_TOKEN` | Optional GitHub token with Pull requests write access for comments and trigger-label removal |
| `OPENAI_MODEL` | Model slug from `wrev models` |

Configure a GitHub App webhook installed on all owned repositories, or add
repository webhooks individually. Send JSON **Pull requests** events to
`https://<domain>/github/webhook` with the same signing secret. Applying
`wrev` creates a queued session. Removing it cancels the queued or running
session. Reapplying it creates a new session. Identical GitHub deliveries are
deduplicated. Each review can run for up to ten minutes.

Set `WREV_GITHUB_COMMENT_TOKEN` to post a start comment and update it with the
findings or terminal status for that review session. Wrev removes the trigger
label after a successful review. Failed reviews keep the label. The token can be scoped
to the repositories where Wrev should comment. Review reads still use
`WREV_GITHUB_TOKEN`. Comment API errors appear in the worker logs, and the
review result remains available in `/admin/reviews`.

To register repository webhooks in bulk, use an authenticated `gh` CLI
credential with permission to administer repository webhooks. Load
`WREV_WEBHOOK_SECRET` into the shell from your secret store and run
`python3 scripts/register-wrev-webhooks.py https://<domain>/github/webhook`.
The script scopes registration to the authenticated user's owned, active
repositories and reuses any webhook already pointed at the same URL.

Use `PUT /admin/chatgpt-credentials` with `Authorization: Bearer <admin-token>`
to store a grant. The JSON body contains `access_token` and `expires_at` in Unix
seconds. Supply both `refresh_token` and the issued `client_id` to enable renewal.
A grant submitted without a refresh token remains usable until its expiry.
For initial testing, an environment `CHATGPT_ACCESS_TOKEN` works before a grant
is stored. `GET /admin/reviews` with the same authorization lists recent
sessions and their results.

The volume holds a durable queue in `queue.sqlite`, review checkpoints in
`sessions-native/<id>/review.sqlite`, and the encrypted ChatGPT grant in
`credentials.sqlite`. On restart, interrupted sessions resume from their last
checkpoint when the PR is still eligible. A checkpoint that records an
unfinished tool call requires a new session. Keep the encryption key available
across deployments and store backups of the volume privately.

Before switching from the Wasmtime worker, drain queued and running reviews.
The previous checkpoint files remain in `sessions/`. The native reviewer uses
a different prompt and tool set, so it starts checkpoints in `sessions-native/`.
