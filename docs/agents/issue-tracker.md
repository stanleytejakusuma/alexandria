# Issue tracker: GitHub (scoped)

Issues for **alexandria engine development** live as GitHub issues in this
repo (`stanleytejakusuma/alexandria`). Use the `gh` CLI for all operations.

## Scope — what belongs here

This tracker is **scoped to development of alexandria itself**: engine
features, bugs, work-order follow-ups, capability pipelines that touch the
engine's own code or its documented ingestion surface.

Do **NOT** put the following here:

- Anything private or credential-bearing (hosts, codenames, gateway keys,
  capital/infra details, agent identities, personal data). These are tracked
  separately, outside GitHub, per operator policy. The leak scanner
  (`.leakpatterns.local`) treats GitHub-facing content as public.
- Capital or live-execution operational items (trading/DeFi fleet, NAS
  daemons, vault operations). Those stay in their own private trackers.
- Backlog items that reference private context. The repo's internal,
  full-context backlog lives in `docs/BACKLOG.md` and `docs/SPEC-*.md`; a
  GitHub issue may reference a public backlog row by number, but must not
  restate private detail.

If a task can't be described publicly without private detail, it does not
get a GitHub issue.

## Conventions

- **Create an issue**: `gh issue create --title "..." --body "..."`. Use a heredoc for multi-line bodies.
- **Read an issue**: `gh issue view <number> --comments`, filtering comments by `jq` and also fetching labels.
- **List issues**: `gh issue list --state open --json number,title,body,labels,comments --jq '[.[] | {number, title, body, labels: [.labels[].name], comments: [.comments[].body]}]'` with appropriate `--label` and `--state` filters.
- **Comment on an issue**: `gh issue comment <number> --body "..."`
- **Apply / remove labels**: `gh issue edit <number> --add-label "..."` / `--remove-label "..."`
- **Close**: `gh issue close <number> --comment "..."`

Infer the repo from `git remote -v`; `gh` does this automatically when run
inside a clone.

## When a skill says "publish to the issue tracker"

Create a GitHub issue **only if** the task is in scope per the rules above.

## When a skill says "fetch the relevant ticket"

Run `gh issue view <number> --comments`.

## Wayfinding operations

Used by `/wayfinder`. The **map** is a single issue with **child** issues as tickets.

- **Map**: a single issue labelled `wayfinder:map`, holding the Notes / Decisions-so-far / Fog body. `gh issue create --label wayfinder:map`.
- **Child ticket**: an issue linked to the map as a GitHub sub-issue (`gh api` on the sub-issues endpoint). Where sub-issues aren't enabled, add the child to a task list in the map body and put `Part of #<map>` at the top of the child body. Labels: `wayfinder:<type>` (`research`/`prototype`/`grilling`/`task`). Once claimed, the ticket is assigned to the driving dev.
- **Blocking**: GitHub's **native issue dependencies**, the canonical, UI-visible representation. Add an edge with `gh api --method POST repos/<owner>/<repo>/issues/<child>/dependencies/blocked_by -F issue_id=<blocker-db-id>`, where `<blocker-db-id>` is the blocker's numeric **database id** (`gh api repos/<owner>/<repo>/issues/<n> --jq .id`, _not_ the `#number` or `node_id`). GitHub reports `issue_dependencies_summary.blocked_by` (open blockers only, the live gate). Where dependencies aren't available, fall back to a `Blocked by: #<n>, #<n>` line at the top of the child body.
- **Frontier query**: list the map's open children (`gh issue list --state open`, scoped to the map's sub-issues / task list), drop any with an open blocker (`issue_dependencies_summary.blocked_by > 0`, or an open issue in the `Blocked by` line) or an assignee; first in map order wins.
- **Claim**: `gh issue edit <n> --add-assignee @me`, the session's first write.
- **Resolve**: `gh issue comment <n> --body "<answer>"`, then `gh issue close <n>`, then append a context pointer (gist + link) to the map's Decisions-so-far.

## Enabled?

Issues are enabled on this repo (`has_issues: true`); no admin action needed.
