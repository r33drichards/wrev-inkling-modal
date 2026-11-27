"""GitHub review status formatting and idempotent comment reconciliation."""
import re
import unicodedata
from urllib.parse import quote

from github_app import trigger_labels

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

# Leave headroom under GitHub's comment size limit, including for Unicode.
COMMENT_BYTES = 60_000
SPECIAL = frozenset(chr(i) for i in range(33, 127) if not chr(i).isalnum())


def plain(value):
    """Render model output as text, not mentions, Markdown, HTML or links."""
    cleaned = " ".join(value.split())
    return "".join(
        f"&#{ord(c)};" if c in SPECIAL else c
        for c in cleaned if unicodedata.category(c) not in {"Cc", "Cf", "Cs"}
    )


def finding_source(finding, repo, head):
    file = finding.get("file")
    first, last = finding.get("start_line"), finding.get("end_line")
    if (not isinstance(file, str) or len(file) > 4096 or "\\" in file
            or any(p in {"", ".", ".."} for p in file.split("/"))
            or any(unicodedata.category(c) in {"Cc", "Cf", "Cs"} for c in file)
            or type(first) is not int or type(last) is not int
            or not 0 < first <= last <= 2**32 - 1):
        return ""
    path = quote(file, safe="/")
    link = f"https://github.com/{repo}/blob/{head}/{path}#L{first}-L{last}"
    # Thousands of percent-encoded multibyte characters are not useful anchors.
    if len(link) > 8_192:
        return ""
    return f"[{plain(file)}:{first}]({link}) · "


def finding_blocks(findings, repo, head):
    for number, finding in enumerate(findings, 1):
        source = finding_source(finding, repo, head)
        message = finding["message"]
        # Chunk unusually long individual findings before escaping (one input
        # code point becomes at most six UTF-8 bytes in plain()).
        for offset in range(0, max(1, len(message)), 4_000):
            label = f"**{number}.** {source}" if not offset else f"**{number} (continued).** "
            yield label + plain(message[offset:offset + 4_000])


def pack_findings(findings, repo, head):
    pages = [[]]
    remaining = COMMENT_BYTES - 512
    for block in finding_blocks(findings, repo, head):
        size = len(block.encode()) + 2
        if size > remaining:
            pages.append([])
            remaining = COMMENT_BYTES - 512
        pages[-1].append(block)
        remaining -= size
    return ["\n\n".join(page) for page in pages]


def eligibility(api, head, base):
    pr = api.get_pull()
    if pr.get("state") != "open" or not trigger_labels(pr):
        return "cancelled"
    if pr.get("head", {}).get("sha") != head or pr.get("base", {}).get("sha") != base:
        return "superseded"
    return "completed"


def finish(api, *, review_id, head, base, app_id, comment_id, outcome, findings=()):
    """Publish verified-revision output via the original App-owned comment."""
    tag = marker(review_id)
    existing = api.get_comment(comment_id)
    if existing is None or not owns(existing, app_id, tag):
        raise RuntimeError("Could not verify the App-owned start comment")
    if outcome == "completed" and existing.get("body", "").startswith(
            heading(STATUS["completed"], head)):
        return outcome  # All pages were posted before marking completion.
    if outcome == "completed":
        outcome = eligibility(api, head, base)
    text = f"{heading(STATUS[outcome], head)}\n"
    if outcome == "completed":
        count = len(findings)
        text += ("\nNo actionable findings reported.\n" if count == 0
                 else f"\n{count} actionable {'finding' if count == 1 else 'findings'} reported.\n")
        main, *extra = pack_findings(findings, api.repo, head)
        if main:
            text += f"\n{main}\n"
        if extra:
            ids = []
            comments = list(api.list_comments())
            for number, body in enumerate(extra, 2):
                outcome = eligibility(api, head, base)
                if outcome != "completed":
                    break
                page_tag = tag.removesuffix(" -->") + f":page:{number} -->"
                page = f"**Wrev findings for `{head[:12]}` ({number}/{len(extra)+1})**\n\n{body}\n\n{page_tag}"
                matches = [c["id"] for c in comments if owns(c, app_id, page_tag)]
                if len(matches) > 1:
                    raise RuntimeError("Duplicate App-owned findings pages")
                ids.append(matches[0] if matches else api.create_comment(page))
            if outcome == "completed":
                outcome = eligibility(api, head, base)
            if outcome == "completed":
                text += f"\n[Continued in {len(ids)} more comment(s)](#issuecomment-{ids[0]}).\n"
            else:
                text = f"{heading(STATUS[outcome], head)}\n"
    text += f"\n{tag}"
    if existing["body"] != text:
        api.edit_comment(comment_id, text)
    return outcome
