# The merge worked, GitHub is behind

## What you see

A task shipped. The live checkout has the commit and the site is running it —
but GitHub's branch is one or more commits behind, so the repository page, a
PR, or a colleague's clone does not show the work.

This is by design, and the design has a sharp edge. The deploy does two
things: a **fast-forward merge** into the live checkout, then a **push** to
`origin`. The merge is the part that must not be rolled back, so the push is
**best effort**: if it fails, the merge is already done and the task is not
failed for it. The result is reported in the response rather than assumed —
but nobody reads a successful task's response.

Found live on 2026-08-23: two agent commits had merged and fully deployed
while GitHub was two commits stale, with nothing anywhere reporting the drift.

---

## Check

**How far behind is each project?** Read-only:

```bash
for d in /home/webapp /home/storefront /home/brochure; do
  git -C "$d" fetch -q origin 2>/dev/null
  b=$(git -C "$d" branch --show-current)
  ahead=$(git -C "$d" rev-list --count origin/$b..$b 2>/dev/null)
  behind=$(git -C "$d" rev-list --count $b..origin/$b 2>/dev/null)
  printf '%-24s %s  local ahead: %s  behind: %s\n' "$d" "$b" "${ahead:-?}" "${behind:-?}"
done
```

`local ahead: 0` everywhere is healthy. Anything above zero is work that is
live but not on GitHub.

**Why did the push fail?** The deploy's own record:

```bash
pm2 logs agent-review --nostream --lines 80 | grep -iE 'push|merge|error'
```

The usual causes:

| Cause | Looks like |
|---|---|
| No deploy key, or the wrong one | `Permission denied (publickey)` |
| Host alias missing from `~/.ssh/config` | `Could not resolve hostname github-<project>` |
| Repository moved (renamed, or into an organization) | `Repository not found`, or a redirect that works for fetch and not for push |
| A branch ruleset rejecting the push | `push declined due to repository rule violations` |
| Network or GitHub outage at that moment | a timeout |

**Is the remote even the one you think?** After an organization move this is
the first thing to check, and `git remote get-url` can lie when a credential
helper rewrites URLs, so read the configured value:

```bash
git -C /home/storefront config --local --get remote.origin.url
```

---

## Act

### Push the backlog by hand

Safe and idempotent. The live checkout is the source of truth here; the merge
already happened:

```bash
cd /home/<project>
git status --porcelain          # expect empty: a dirty tree means look first
git push origin "$(git branch --show-current)"
```

If that prints `Everything up-to-date`, GitHub already had it and the drift
was in your reading, not the repo.

### Fix the cause, not just the backlog

- **Key problems:** Settings → Projects → the project → deploy key. The card
  generates a key, shows the public half to paste into GitHub, and tests the
  connection. The private half never crosses the API.
- **A moved repository:** update the remote to the new path (do not rely on
  GitHub's redirect, which does not cover every push), then re-add the deploy
  key on the moved repository — keys do not always travel with a transfer.
- **A ruleset:** a fast-forward push to the default branch is what the deploy
  does. A rule requiring pull requests, or blocking direct pushes, will refuse
  every deploy. Either scope the rule to exclude this path or accept that the
  agent's work lands on GitHub by hand.

### Prove it is fixed

Re-run the first block on this page. Every project should read `local ahead:
0`. Do not take a single successful `git push` as proof for the others.

---

## What not to do

- **Do not force-push to "catch GitHub up".** The live checkout and GitHub
  share history; a force-push rewrites it for everyone, and the deploy path is
  fast-forward only precisely so this never has to happen.
- **Do not re-run the task.** The work is merged. Re-running spends money to
  produce a diff that is already in the tree.
- **Do not commit directly on the live checkout** to "help" a pending agent
  task — the merge is fast-forward only, so a commit on the live branch makes
  every pending agent branch un-mergeable until it is rebased.
