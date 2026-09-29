"""The desktop app (app/) and the release pipeline that feeds it.

The app pulls released images rather than building from source, so three
lists must agree: the images the release workflow publishes, the local
names the compose file gives them, and the names the app's Rust backend
pulls and tags. And the stack files the app bundles must be the ones the
compose file references at runtime, or the stack starts without them."""
import json
import re

import yaml

from agent import paths

REPO = paths.REPO_ROOT
IMAGES = {"agent", "router", "reviewer", "sandbox"}


def test_the_release_workflow_publishes_every_image_the_compose_file_names():
    wf = yaml.safe_load((REPO / ".github/workflows/release.yml").read_text())
    matrix = {row["image"] for row in wf["jobs"]["images"]["strategy"]["matrix"]["include"]}
    assert matrix == IMAGES
    compose = yaml.safe_load((REPO / "docker-compose.yml").read_text())
    named = {svc.get("image") for svc in compose["services"].values() if "build" in svc}
    assert named == {f"tektonix-{n}:latest" for n in IMAGES}
    # packages: write belongs to the job that pushes, not to the workflow:
    # the installer job holds the updater's signing key and needs no
    # registry access.
    assert "packages" not in wf["permissions"]
    assert wf["jobs"]["images"]["permissions"]["packages"] == "write"
    assert "app-windows" in wf["jobs"] and wf["jobs"]["app-windows"]["needs"] == "images"


def test_the_app_pulls_the_same_images_and_tags_them_with_the_compose_names():
    rust = (REPO / "app/src-tauri/src/stack.rs").read_text()
    m = re.search(r'pub const IMAGES: \[&str; \d\] = \[(.*?)\];', rust)
    assert m and set(re.findall(r'"([a-z]+)"', m.group(1))) == IMAGES
    assert 'pub const REGISTRY: &str = "ghcr.io/djg3dk";' in rust
    assert 'format!("tektonix-{name}:latest")' in rust


def test_the_app_bundles_every_stack_file_the_compose_file_binds():
    conf = json.loads((REPO / "app/src-tauri/tauri.conf.json").read_text())
    resources = conf["bundle"]["resources"]
    for src in resources:
        assert (REPO / "app/src-tauri" / src).exists(), f"{src} is bundled but missing"
    shipped = {v.removeprefix("stack/") for v in resources.values()}
    rust = (REPO / "app/src-tauri/src/stack.rs").read_text()
    m = re.search(r"const SHIPPED: \[&str; \d\] = \[(.*?)\];", rust, re.S)
    assert m and set(re.findall(r'"([^"]+)"', m.group(1))) == shipped, "stack.rs copies what tauri.conf.json bundles"
    compose_text = (REPO / "docker-compose.yml").read_text()
    for path in re.findall(r"- \./(docker/[\w./-]+\.sh):", compose_text):
        assert path in shipped, f"the compose file binds {path}; the app must ship it"
    assert "services/model-router/config.example.yaml" in shipped, "the router's seed"
    assert conf["plugins"]["updater"]["pubkey"] != "REPLACED_AT_KEY_GENERATION"
    assert conf["bundle"]["createUpdaterArtifacts"] is True


def test_the_app_checks_the_agents_password_rules_before_spending_the_one_time_password():
    """2026-09-29: the panel checked only the length. A long all-lowercase
    passphrase passed it, the agent refused it, and by then the one-time
    password had been read (which deletes it): no way in. The app and the
    panel now carry the agent's rules, in the agent's words, so a new rule
    in auth.py fails here until both copies know it."""
    from agent.auth import validate_password_strength

    tripping = ["Short1a", "correct horse battery staple", "CORRECT HORSE BATTERY 1", "Correct Horse Battery"]
    messages = {validate_password_strength(p) for p in tripping}
    assert None not in messages and len(messages) == 4, "one input per rule"
    assert validate_password_strength("Correct Horse Battery 1") is None
    rust = (REPO / "app/src-tauri/src/stack.rs").read_text()
    panel = (REPO / "app/src/main.js").read_text()
    for msg in messages:
        assert f'"{msg}"' in rust, f"stack.rs password_problem lacks: {msg}"
        assert f'"{msg}"' in panel, f"main.js passwordProblem lacks: {msg}"
    body = rust.split("pub async fn set_first_password", 1)[1]
    assert body.index("password_problem(password)") < body.index("initial_password(app)"), "the rules run before the one-time password is read"
    assert "with_one_time_password(" in body, "a failure after the read hands the one-time password back"


def test_the_app_version_is_one_number_in_three_places():
    conf = json.loads((REPO / "app/src-tauri/tauri.conf.json").read_text())
    pkg = json.loads((REPO / "app/package.json").read_text())
    cargo = (REPO / "app/src-tauri/Cargo.toml").read_text()
    m = re.search(r'^version = "([^"]+)"', cargo, re.M)
    assert conf["version"] == pkg["version"] == m.group(1)
