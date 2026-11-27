"""GitHub review status formatting and idempotent comment reconciliation."""
import re

STATUS = {
    "completed": "Wrev review completed",
    "failed": "Wrev review failed",
    "cancelled": "Wrev review cancelled",
    "superseded": "Wrev review superseded",
}
def marker(review_id):
    if not re.fullmatch(r"[1-9][0-9]{0,18}|[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", str(review_id)):
        raise ValueError("invalid review ID")
    return f"<!-- mwrev-review:{review_id} -->"

def heading(title, head):
    if not re.fullmatch(r"[0-9a-f]{40}", head):
        raise ValueError("invalid review revision")
    return f"**{title}** for `{head[:12]}`."

def start(api, *, review_id, head, app_id, comment_id=None):
    """Call under the caller's per-PR publication lock.

    The injected API provides paginated comments and a scoped App token.
    API errors, including ambiguous POST acknowledgement, must propagate.
    Before retrying, the caller must persist/observe an uncertainty interval.
    """
    tag = marker(review_id)
    if comment_id is not None:
        existing = api.get_comment(comment_id)
        if existing is not None and owns(existing, app_id, tag):
            return comment_id
        raise RuntimeError("Stored start comment is missing or not app-owned")
    matches = [c["id"] for c in api.list_comments() if owns(c, app_id, tag)]
    if len(matches) > 1:
        raise RuntimeError("Duplicate app comments require intervention")
    if matches:
        return matches[0]
    return api.create_comment(f"{heading('Wrev review started', head)}\n\n{tag}")

def owns(comment, app_id, tag):
    app = comment.get("performed_via_github_app") or {}
    return app_id == app.get("id") and tag in comment.get("body", "").splitlines()

def finish(api, *, review_id, head, app_id, comment_id, outcome, findings_count=0):
    """Edit only the matching App-owned comment. Never POST on completion."""
    tag = marker(review_id)
    existing = api.get_comment(comment_id)
    if existing is None or not owns(existing, app_id, tag):
        raise RuntimeError("Could not verify the App-owned start comment")
    if outcome == "completed" and existing.get("body", "").startswith(
            heading(STATUS["completed"], head)):
        return outcome  # Publication succeeded on an earlier attempt.
    if outcome == "completed":
        pr = api.get_pull()
        labels = {label["name"] for label in pr.get("labels", [])}
        if pr.get("state") != "open" or "mwrev" not in labels:
            outcome = "cancelled"
        elif pr.get("head", {}).get("sha") != head:
            outcome = "superseded"
    if outcome == "completed" and (type(findings_count) is not int or findings_count < 0):
        raise ValueError("invalid findings count")
    text = f"{heading(STATUS[outcome], head)}\n"
    if outcome == "completed":
        text += ("\nNo actionable findings reported.\n" if findings_count == 0
                 else f"\n{findings_count} actionable findings reported.\n")
    text += f"\n{tag}"
    if existing["body"] != text:
        api.edit_comment(comment_id, text)
    return outcome
