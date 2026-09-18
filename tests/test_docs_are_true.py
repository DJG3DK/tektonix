"""The map and the runbooks have to stay true, or they are worse than nothing.

These check the handful of facts that a reader will act on and that change
silently: the ports, the health routes, the secret files, the graph's shape.
Prose is not tested -- claims a reader would follow are.
"""
import pathlib
import re

import pytest

DOCS = pathlib.Path("docs")
ARCH = DOCS / "architecture.md"


def test_the_map_and_runbooks_exist():
    assert ARCH.exists()
    for page in ("README.md", "stuck-task.md", "consolidation.md",
                 "router-refusals.md", "merge-vs-github.md"):
        assert (DOCS / "runbooks" / page).exists(), page
    assert (DOCS / "backup.md").exists()


def test_every_internal_link_resolves():
    bad = []
    pages = list(DOCS.rglob("*.md")) + [pathlib.Path(p) for p in ("README.md", "INSTALL.md", "CONTRIBUTING.md")]
    for md in pages:
        for m in re.finditer(r"\[([^\]]+)\]\(([^)#][^)]*)\)", md.read_text()):
            target = m.group(2).split("#")[0]
            if target.startswith(("http", "mailto:")):
                continue
            if not (md.parent / target).resolve().exists():
                bad.append(f"{md} -> {target}")
    assert not bad, f"broken documentation links: {bad}"


def test_the_map_names_the_ports_the_code_actually_uses():
    """A wrong port sends a reader to the wrong process at the worst moment."""
    text = ARCH.read_text()
    for port in ("8100", "4001", "4100", "4101"):
        assert port in text, f"architecture.md does not mention port {port}"
    assert "4101" in pathlib.Path("services/commit-reviewer/reviewer.js").read_text()
    assert "4100" in pathlib.Path("services/agent-review/server.js").read_text()


def test_operator_docs_send_health_checks_to_the_router_s_real_port():
    """The LiteLLM proxy was :4000. The router has been :4001 since the
    cutover. A troubleshooting curl at the old port looks like a down
    router when the process is healthy."""
    install = pathlib.Path("INSTALL.md").read_text()
    playbook = pathlib.Path("docs/playbooks/add-a-managed-role.md").read_text()
    assert "127.0.0.1:4001/health/liveliness" in install
    assert "127.0.0.1:4000/health/liveliness" not in install
    assert "127.0.0.1:4001/v1/models" in playbook
    assert "127.0.0.1:4000/v1/models" not in playbook


def test_the_review_secret_example_names_the_file_the_services_read():
    """The Node services read REVIEW_CONTROL_SECRET from services/shared/.env.
    .env.example used to tell a hand-rolled install to put it in the
    router's .env -- the path that made the model proxy a secrets bus."""
    text = pathlib.Path(".env.example").read_text()
    # The assignment line, plus the comment block immediately above it.
    key = text.index("\nREVIEW_CONTROL_SECRET=")
    block = text[text.rfind("\n\n", 0, key):key]
    assert "services/shared/.env" in block
    assert "services/model-router/.env" not in block


def test_the_installer_nginx_vhost_exposes_the_review_dashboard():
    """The review UI's mutating routes require X-Review-Secret. The browser
    never holds it; nginx injects it on /_review/. A vhost without that
    location makes Check now / merge / restart 401 after a by-the-book
    install."""
    text = pathlib.Path("install.sh").read_text()
    assert "location /_review/" in text
    assert "X-Review-Secret" in text
    assert "location /_review/" in pathlib.Path("INSTALL.md").read_text()


def test_vision_falls_back_to_the_router_s_real_port():
    text = pathlib.Path("agent/tools/vision.py").read_text()
    assert "127.0.0.1:4001/v1" in text
    assert "127.0.0.1:4000/v1" not in text


def test_the_health_routes_the_docs_promise_exist_in_the_code():
    assert '@app.get("/api/health")' in pathlib.Path("agent/server.py").read_text()
    assert "app.get('/health'" in pathlib.Path("services/agent-review/server.js").read_text()
    assert "'/health'" in pathlib.Path("services/commit-reviewer/reviewer.js").read_text()


def test_the_map_points_at_the_real_secret_files():
    """The review secret moved out of the router's .env; the map must say so,
    and both services must really read it from the shared module."""
    text = ARCH.read_text()
    assert "services/shared/.env" in text
    for service in ("services/agent-review/server.js", "services/commit-reviewer/reviewer.js"):
        assert "readServiceSecret" in pathlib.Path(service).read_text(), service


def test_the_graph_really_is_the_two_nodes_the_map_describes():
    graph = pathlib.Path("agent/outer_graph.py").read_text()
    nodes = set(re.findall(r'add_node\(\s*"([a-z_]+)"', graph))
    assert nodes == {"work", "verify_and_ship"}, f"the map says two nodes; the graph has {nodes}"
    assert "work" in ARCH.read_text() and "verify_and_ship" in ARCH.read_text()


@pytest.mark.parametrize("script", ["scripts/backup.sh", "scripts/verify_backup_restore.sh"])
def test_the_backup_scripts_exist_and_are_executable(script):
    p = pathlib.Path(script)
    assert p.exists(), script
    assert p.stat().st_mode & 0o111, f"{script} is not executable"
    assert script in pathlib.Path("docs/backup.md").read_text()


def test_the_landing_page_does_not_advertise_review_services_the_bundle_lacks():
    """The public page sold the review gate as part of `docker compose up`.
    compose runs postgres, router and agent. docker/README.md already said
    the review services are not in the bundle; the landing page did not."""
    compose = pathlib.Path("docker-compose.yml").read_text()
    landing = pathlib.Path("site/src/LandingPage.tsx").read_text()
    readme = pathlib.Path("docker/README.md").read_text()
    has_review = "agent-review" in compose or "commit-reviewer" in compose
    if has_review:
        return
    assert "review services as one stack" not in landing
    assert "not in this bundle yet" in landing
    assert "What is not in the bundle yet" in readme
    assert "review services" in readme.lower()


def test_the_doctor_and_release_scripts_exist_and_are_documented():
    """Slice 2: a layout diagram nobody can check is decoration."""
    for script in ("scripts/doctor.py", "scripts/package_release.sh"):
        p = pathlib.Path(script)
        assert p.exists(), script
        assert p.stat().st_mode & 0o111, f"{script} is not executable"
    install = pathlib.Path("INSTALL.md").read_text()
    assert "scripts/doctor.py" in install
    assert "package_release.sh" in install
    assert "Where every secret lives" in install


def test_the_secret_diagram_names_every_file_the_doctor_checks():
    """If the doctor learns about a new secret file, the diagram must too --
    they are the same claim, one checked and one read."""
    import importlib.util
    import sys as _sys
    spec = importlib.util.spec_from_file_location("doctor_doc", pathlib.Path("scripts/doctor.py"))
    doctor = importlib.util.module_from_spec(spec)
    _sys.modules["doctor_doc"] = doctor
    spec.loader.exec_module(doctor)

    install = pathlib.Path("INSTALL.md").read_text()
    # Relative to the install root, not the checkout folder name. The old
    # check used parent.name for `.env`, which is `3d-agent` on the
    # maintainer's box and `tektonix` in CI -- both happen to appear in
    # INSTALL.md -- and `workspace` anywhere else. The files are the claim.
    for path in (doctor.AGENT_ENV, doctor.ROUTER_ENV, doctor.SHARED_ENV, doctor.PROJECTS_JSON):
        rel = path.relative_to(doctor.ROOT).as_posix()
        assert rel in install, f"INSTALL.md does not mention {rel}"
    assert "review-secrets" in install and "keys/" in install


# ---------------------------------------------------------------------------
# The playbooks (docs/playbooks/) and the middleware inventory
# ---------------------------------------------------------------------------

PLAYBOOKS = DOCS / "playbooks"


def test_the_playbooks_and_the_inventory_exist():
    assert (DOCS / "middleware.md").exists()
    for page in ("README.md", "add-a-managed-role.md", "add-an-inbox-source.md",
                 "add-a-runtime-knob.md"):
        assert (PLAYBOOKS / page).exists(), page


@pytest.mark.parametrize("page", sorted(p.name for p in (DOCS / "playbooks").glob("add-*.md")))
def test_every_playbook_opens_with_a_test_that_fails_first(page):
    """The premise of a playbook here is that you do not have to trust the
    checklist: something red tells you what is still unwired. A playbook
    naming a test that does not exist is back to a checklist."""
    text = (PLAYBOOKS / page).read_text()
    named = re.findall(r"tests/test_\w+\.py", text)
    assert named, f"{page} names no test to run first"
    for rel in set(named):
        assert pathlib.Path(rel).exists(), f"{page} points at {rel}, which does not exist"


def _has_example(ref: str) -> bool:
    """Some files a playbook tells you to edit are the operator's, not the
    repo's -- the router config is gitignored because the Models page
    rewrites it. What ships is the example beside it, and that is what has to
    exist for the instruction to be followable on a fresh clone."""
    path = pathlib.Path(ref)
    return (path.with_suffix(f".example{path.suffix}").exists()
            or pathlib.Path(f"{ref}.example").exists())


@pytest.mark.parametrize("page", sorted(p.name for p in (DOCS / "playbooks").glob("*.md")))
def test_every_file_a_playbook_tells_you_to_edit_is_really_there(page):
    """Each step names a file. A renamed module turns the playbook into a
    treasure hunt, and the reader has no way to tell which half is stale."""
    text = (PLAYBOOKS / page).read_text()
    missing = []
    for ref in re.findall(r"`((?:agent|frontend|services|tests|scripts)/[\w./-]+)`", text):
        if ref.endswith("/"):
            continue
        if not pathlib.Path(ref).exists() and not _has_example(ref):
            missing.append(ref)
    assert not missing, f"{page} names files that do not exist: {sorted(set(missing))}"


def test_the_inventory_is_reachable_from_the_map():
    """A page nobody links to is a page nobody reads."""
    arch = ARCH.read_text()
    assert "middleware.md" in arch, "docs/architecture.md does not link the middleware inventory"
    assert "playbooks" in arch, "docs/architecture.md does not link the playbooks"


def test_the_readme_describes_the_lock_that_actually_exists():
    """The opening section said one task per project was "enforced by an
    in-process lock". It was, once. It has been a Postgres session-level
    advisory lock since the in-process version was found to be true only
    while exactly one process existed -- and the whole point of the change is
    that a reader must not believe the old sentence."""
    readme = pathlib.Path("README.md").read_text()
    graph = pathlib.Path("agent/graph.py").read_text()
    assert "pg_try_advisory_lock" in graph, "the lock is no longer a Postgres advisory lock"
    assert "advisory lock" in readme, "the README does not say how one-task-per-project is enforced"
    assert "enforced by an in-process lock" not in readme


def test_every_image_a_doc_shows_actually_exists():
    """Links were checked; images were not, and that is the gap that bit.

    2026-09-18: splitting the landing page into site/ moved the seven
    screenshots the README embeds, and nothing noticed. GitHub renders a
    missing image as a broken icon rather than an error, so the README looked
    fine in every diff and wrong on the page for a day.

    Relative paths only -- an absolute URL points at something this repo does
    not own and cannot check without the network.
    """
    import re

    root = pathlib.Path(__file__).resolve().parent.parent
    broken: list[str] = []
    for md in [root / "README.md", root / "INSTALL.md", root / "CONTRIBUTING.md",
               root / "SECURITY.md", *(root / "docs").rglob("*.md")]:
        if not md.is_file():
            continue
        text = md.read_text()
        refs = re.findall(r'<img[^>]+src="([^"]+)"', text)
        refs += [m for m in re.findall(r"!\[[^\]]*\]\(([^)]+)\)", text)]
        for ref in refs:
            if ref.startswith(("http://", "https://", "data:")):
                continue
            target = (root if md.parent == root else md.parent) / ref.split("#")[0]
            if not target.is_file():
                broken.append(f"{md.relative_to(root)} -> {ref}")
    assert not broken, "images a doc shows but the repo does not have: " + ", ".join(broken)
