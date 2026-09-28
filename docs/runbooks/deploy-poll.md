# Deploying a project whose main is merged elsewhere

`scripts/deploy-poll.sh` pulls a project's GitHub main when it moves and
deploys it on this machine: install dependencies when the package files
changed, run the build, restart the named pm2 processes. The agent deploys
only its own merges; this notices everyone else's, such as a pull request
merged on GitHub or a task shipped from another machine.

One cron line per project, every two minutes:

```
*/2 * * * * PROJECT_DIR=/srv/app PM2_APPS="app app-worker" KEEP_DIRTY="config/state.json" /path/to/tektonix/scripts/deploy-poll.sh >/dev/null 2>&1
```

* `KEEP_DIRTY` names tracked files the running app rewrites. They are never
  reset, and a commit on main that also changes one stops the deploy with
  "merge that by hand": state and code disagree, and a script must not pick.
* `package-lock.json` (or `RESET_DIRTY`) is regenerated on install and is
  thrown away when main brings a new one.
* Any other local edit on a file main changed stops the deploy; commit or
  discard it. Local edits on files main did not touch are left alone.
* `BUILD_CMD` overrides the default `npm run build`; set it empty to skip.

What happened is in `data/auto-deploy.log` (or `logs/`) under the project:
one line per deploy with the commit range, or the reason it stopped. When
nothing changed it prints nothing there. `tests/test_deploy_poll.sh` runs
the script against a real git pair with pm2 and npm stubbed.
