# ReviewBench methodology

## Corpus and inputs

Both reviewers ran on the same 219 pull requests in the
[ReviewBench full corpus](https://github.com/review-bench/ReviewBench/blob/082f65f6746c39b6ac25b0a62c1b0d5131a3803a/corpus/manifest.json).
The manifest pins each PR's repository, number, base commit, and head commit.
Each run produced one accepted findings file per PR. The runner validates the
PR identity and findings format, and an empty findings array counts as a
completed review. Failed tasks were retried for full coverage.

## Reviewer setup

| Reviewer | Model | Harness |
| --- | --- | --- |
| Wrev | GPT-6 Luna (`gpt-6-luna`) | Native Rust reviewer with embedded QuickJS and read-only filesystem tools |
| Codex CLI | GPT-6 Luna (`gpt-6-luna`) | Codex CLI v0.159.3 using its review command inside the ReviewBench container |

The Wrev run used the filesystem changes later merged in
[PR #15](https://github.com/r33drichards/wrev/pull/15). Wrev received the pinned
head and base trees, PR metadata, and diff through the snapshot filesystem.
Its [review instructions](https://github.com/r33drichards/wrev/blob/cc47a43540a145a047c5d2d79fb1cfa20b9cc3db/src/review_prompt.md)
asked it to inspect the complete diff and check code quality, security,
performance, testing, and documentation.

The Codex adapter ran the CLI against the pinned base with the head checked
out. Both used the local ReviewBench harness based on commit
`082f65f6746c39b6ac25b0a62c1b0d5131a3803a`, with local runner and judge adapter
changes. The table reports one completed run per reviewer. It compares the
reviewers as configured, including their prompts and tools.

## Scoring

The [ReviewBench offline judge](https://github.com/review-bench/ReviewBench/blob/082f65f6746c39b6ac25b0a62c1b0d5131a3803a/docs/OFFLINE_EVAL.md)
used GPT-6.1 Terra (`openai-codex/gpt-6.1-terra`) for both matching and
classification. It first matched submitted findings against the golden set,
then classified unmatched submissions as true or false positives. The runs had
identical fingerprints for the evaluated PRs, golden set, and both judge
prompts. Both were scored on all 219 PRs.

The README reports micro-averages, computed from counts pooled over the corpus.

| Metric | Calculation |
| --- | --- |
| Grounded precision | Matched true-positive submissions divided by all matched submissions |
| Grounded recall | Distinct golden true positives covered divided by all golden true positives |
| Augmented precision | Matched true-positive submissions plus novel true positives, divided by all submissions |
| Augmented recall | Golden true positives covered plus novel true positives, divided by all golden true positives plus novel true positives |
| False positives (% of submissions) | Matched and unmatched false-positive submissions divided by all submissions |
| Novel true positives | Unmatched submissions classified as true positives by Terra |

For another comparison, keep the manifest and judge configuration fixed.
Record the reviewer build and model, preserve per-PR outputs, and verify the
scored PR count and evaluation fingerprints before comparing metrics.
