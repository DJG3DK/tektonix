# Tektonix desktop

The dashboard in a window, the compose stack under it, and a tray icon to
reach both. A [Tauri 2](https://tauri.app) shell: the window's own page
(`src/`) is a small control panel, the dashboard is the agent's web app
opened in a second window once the stack answers, and everything that does
work is Rust (`src-tauri/src/`).

What it does that the console installer did not:

* installs WSL 2 and Docker Desktop when they are missing (Windows), through
  Windows' own permission prompt;
* asks for the two settings in a form, keeps them in its own `.env`;
* pulls the release's images from GHCR instead of building from source, with
  Docker's progress in the window, and starts the stack with `--no-build`;
* shows the first password in the window;
* updates the stack in one click when a new release is out, and updates
  itself through Tauri's signed updater.

## Layout on the machine

Under the app's local data directory (`%LOCALAPPDATA%\Tektonix` on Windows):

```
stack/docker-compose.yml           copied from the app on every launch
stack/docker/…                     (so an app update carries the stack's
stack/services/model-router/…       own files with it)
stack/.env                         the operator's; written by setup, never overwritten
stack/version.json                 which release's images are installed
```

Images come from `ghcr.io/djg3dk/tektonix-<name>:<tag>` and are tagged with
the local names the compose file uses (`tektonix-<name>:latest`), so one
compose file serves both this app and a source checkout.

## Building

The Windows installer is built by `.github/workflows/release.yml` on a
version tag, signed for the updater with the key in the repository's
secrets, and attached to the GitHub release with `latest.json`.

Locally, without a Rust toolchain on the machine, the backend type-checks
and its tests run in Tauri's own container:

```bash
docker run --rm -v "$PWD:/repo" -w /repo/app/src-tauri ivangabriele/tauri:debian-bookworm-20 \
  sh -c "cargo check && cargo test"
```

A full Windows build needs Windows (or the workflow). `npm ci && npm run
build` in `app/` does it there.
