"""Detection + provisioning guards for the project onboarding wizard.

The highest-value test in this file is
test_test_script_hitting_a_live_service_is_flagged_and_disabled: it encodes
the incident this deployment already lived through -- a test suite that
POSTed real trade orders at a live bot -- so onboarding can never silently
enable that class of script again.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

from agent import provisioning as prov


@pytest.fixture(autouse=True)
def _allow_tmp_as_project_root(monkeypatch, tmp_path):
    """Detection enforces AGENT_PROJECT_ROOTS containment (see
    test_onboarding_security.py). These unit tests build their fixtures under
    tmp_path, so allow it explicitly rather than weakening the default."""
    monkeypatch.setenv("AGENT_PROJECT_ROOTS", str(tmp_path))
    monkeypatch.setenv("AGENT_SANDBOX_ROOT", str(tmp_path / "workspaces"))


def _git_init(path: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=path, check=True)
    (path / "README.md").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=path, check=True)


@pytest.fixture
def node_repo(tmp_path):
    repo = tmp_path / "shop-api"
    repo.mkdir()
    (repo / "package.json").write_text(json.dumps({
        "name": "shop-api",
        "scripts": {
            "typecheck": "tsc --noEmit",
            "lint": "eslint src",
            "build": "vite build",
            "test": "vitest run",
        },
    }))
    (repo / "package-lock.json").write_text("{}")
    (repo / ".gitignore").write_text(".env\nconfig/keys.json\ndata/fixtures\nnode_modules\n")
    (repo / ".env").write_text("DATABASE_URL=postgres://localhost/x\n")
    (repo / "config").mkdir()
    (repo / "config" / "keys.json").write_text("{}")
    (repo / "data" / "fixtures").mkdir(parents=True)
    (repo / "data" / "fixtures" / "sample.json").write_text("[]")
    _git_init(repo)
    return repo


def test_detects_stack_scripts_and_build_steps(node_repo):
    r = prov.detect_project(str(node_repo))
    assert r.blockers == []
    assert r.is_git_repo and r.package_manager == "npm"
    assert "node" in r.languages
    names = [c["name"] for c in r.checks]
    assert names == ["typecheck", "lint", "build", "test"]
    assert all(c["cmd"] == "npm" for c in r.checks)
    # install always precedes build in the deploy steps
    assert r.build_steps[0]["args"][0] == "install"
    assert r.build_steps[-1]["args"] == ["run", "build"]


def test_gitignored_credentials_are_proposed_and_fixtures_default_off(node_repo):
    r = prov.detect_project(str(node_repo))
    secrets = {c.value: c for c in r.secret_files}
    assert ".env" in secrets and "config/keys.json" in secrets
    assert all(c.enabled for c in r.secret_files), "secrets are needed for checks to be real"
    mounts = {c.value: c for c in r.read_only_mounts}
    assert "data/fixtures" in mounts
    assert mounts["data/fixtures"].enabled is False, "mounting host dirs must be opt-in"
    # a gitignored env carrying DATABASE_URL wires the project_db tool
    assert r.db_env_file == ".env"


def test_test_script_hitting_a_live_service_is_flagged_and_disabled(tmp_path):
    """The my-service lesson: test:routes POSTed real orders at a live service."""
    repo = tmp_path / "trader"
    repo.mkdir()
    (repo / "tests").mkdir()
    (repo / "tests" / "test_routes.js").write_text(
        "await fetch('http://a live service/trade/open', {method:'POST'});\n")
    (repo / "tests" / "test_pure.js").write_text("assert(1+1===2);\n")
    (repo / "package.json").write_text(json.dumps({
        "scripts": {
            "test:routes": "node tests/test_routes.js",
            "test:pure": "node tests/test_pure.js",
            "test": "npm run test:routes && npm run test:pure",
        },
    }))
    (repo / "package-lock.json").write_text("{}")
    _git_init(repo)

    r = prov.detect_project(str(repo))
    flagged = {c.value: c for c in r.risky_scripts}
    assert "test:routes" in flagged, "a test making live HTTP calls must be flagged"
    assert "test:pure" not in flagged, "pure logic tests must not be flagged"
    assert flagged["test:routes"].enabled is False
    assert flagged["test:routes"].warning
    # with no curated safe list, the aggregate `test` script must NOT be
    # auto-enabled -- it chains the dangerous one.
    assert "test" not in [c["name"] for c in r.checks]
    assert any("network calls" in w for w in r.warnings)


def test_repo_declaring_test_review_is_trusted_over_the_aggregate(tmp_path):
    repo = tmp_path / "curated"
    repo.mkdir()
    (repo / "tests").mkdir()
    (repo / "tests" / "live.js").write_text("fetch('http://127.0.0.1:9/x')\n")
    (repo / "package.json").write_text(json.dumps({
        "scripts": {"test:live": "node tests/live.js",
                    "test:review": "node tests/pure.js",
                    "test": "npm run test:live"},
    }))
    (repo / "package-lock.json").write_text("{}")
    _git_init(repo)
    r = prov.detect_project(str(repo))
    test_check = next(c for c in r.checks if c["name"] == "test")
    assert test_check["args"] == ["run", "test:review"], "the repo's own safe list wins"


def test_monorepo_workspaces_expand_to_node_modules_dirs_and_builds(tmp_path):
    repo = tmp_path / "mono"
    (repo / "apps" / "api").mkdir(parents=True)
    (repo / "apps" / "web").mkdir(parents=True)
    (repo / "package.json").write_text(json.dumps({
        "workspaces": ["apps/*"], "scripts": {"build": "turbo build"}}))
    (repo / "pnpm-lock.yaml").write_text("")
    (repo / "apps" / "api" / "package.json").write_text(json.dumps({"scripts": {"build": "nest build"}}))
    (repo / "apps" / "web" / "package.json").write_text(json.dumps({"scripts": {}}))
    _git_init(repo)
    r = prov.detect_project(str(repo))
    assert r.package_manager == "pnpm"
    assert r.node_modules_dirs == [".", "apps/api", "apps/web"]
    assert r.build_steps[0]["args"] == ["install", "--frozen-lockfile"]
    build_dirs = [s["dir"] for s in r.build_steps if s["args"][:1] == ["run"]]
    assert build_dirs == [".", "apps/api"], "only packages with a build script"


def test_python_project_gets_pytest_checks(tmp_path):
    repo = tmp_path / "pyproj"
    (repo / "tests").mkdir(parents=True)
    (repo / "pyproject.toml").write_text("[project]\nname='x'\n")
    _git_init(repo)
    r = prov.detect_project(str(repo))
    assert "python" in r.languages
    assert [c["name"] for c in r.checks] == ["test"]
    assert r.checks[0]["args"] == ["-m", "pytest", "-q"]


def test_non_git_directory_is_a_blocker(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    r = prov.detect_project(str(plain))
    assert any("not a git repository" in b for b in r.blockers)


def test_duplicate_name_is_a_blocker(node_repo):
    r = prov.detect_project(str(node_repo), existing_names=["shop-api"])
    assert any("already configured" in b for b in r.blockers)


def test_relative_path_is_rejected():
    with pytest.raises(prov.ProvisioningError):
        prov.detect_project("./somewhere")


def test_project_with_no_manifest_warns_but_does_not_block(tmp_path):
    repo = tmp_path / "bare"
    repo.mkdir()
    _git_init(repo)
    r = prov.detect_project(str(repo))
    assert r.blockers == []
    assert any("no recognized project manifest" in w for w in r.warnings)
    assert any("no automated checks" in w for w in r.warnings)


# --- provisioning ---------------------------------------------------------

def test_config_from_choices_uses_operator_answers_not_detection(node_repo):
    """An operator's rejection must survive: detection proposed .env AND
    config/keys.json; the operator kept only .env."""
    entry = prov.config_from_choices(
        "shop-api", str(node_repo), "/tmp/wt/shop-api",
        {"secret_files": [".env"], "checks": [{"name": "lint", "dir": ".", "cmd": "npm",
                                               "args": ["run", "lint"]}],
         "pm2_apps": ["shop"], "build_steps": [{"dir": ".", "cmd": "npm", "args": ["ci"]}],
         "node_modules_dirs": ["."], "db_env_file": ".env"},
    )
    assert entry["review"]["secretFiles"] == [".env"]
    assert entry["deploy"]["pm2Apps"] == ["shop"]
    assert entry["db_env_file"] == ".env"
    assert "readOnlyMounts" not in entry["review"], "empty choices stay absent, not empty lists"


def test_write_project_entry_is_atomic_and_refuses_duplicates(tmp_path):
    p = tmp_path / "projects.json"
    p.write_text(json.dumps({"projects": {"a": {"live": "/a", "sandbox": "/s/a"}}}))
    prov.write_project_entry(p, "b", {"live": "/b", "sandbox": "/s/b"})
    data = json.loads(p.read_text())
    assert set(data["projects"]) == {"a", "b"}
    assert not list(tmp_path.glob("*.tmp")), "temp file must be renamed away"
    with pytest.raises(prov.ProvisioningError):
        prov.write_project_entry(p, "b", {"live": "/b", "sandbox": "/s/b"})


def test_create_worktree_makes_a_real_worktree(node_repo, tmp_path):
    sandbox = tmp_path / "workspaces" / "shop-api"
    ok, out = prov.create_worktree(str(node_repo), str(sandbox))
    assert ok, out
    assert (sandbox / "README.md").is_file()
    # a worktree's .git is a POINTER FILE, not a directory -- the property the
    # sandbox mount logic and tool_errors handling both depend on
    assert (sandbox / ".git").is_file()
    # idempotent: re-running reports the existing worktree rather than failing
    ok2, out2 = prov.create_worktree(str(node_repo), str(sandbox))
    assert ok2 and "already exists" in out2


def test_create_worktree_refuses_to_clobber_a_non_worktree_dir(node_repo, tmp_path):
    sandbox = tmp_path / "occupied"
    sandbox.mkdir()
    (sandbox / "important.txt").write_text("do not delete me")
    ok, out = prov.create_worktree(str(node_repo), str(sandbox))
    assert not ok and "refusing to overwrite" in out
    assert (sandbox / "important.txt").is_file()


# ---------------------------------------------------------------------------
# Other stacks (Slice 3)
#
# The npm path has been the only one that detected real commands; Go, Rust,
# Ruby and pytest were recognized as "a manifest exists" and nothing more. A
# repo onboarded that way has an empty checks list, which makes the review
# gate a no-op -- it passes every change because it runs nothing. These tests
# hold the three properties that matter for the new stacks: the commands are
# the repo's own, a suite that calls the network still arrives disabled, and
# the client still cannot author what the review service executes.
# ---------------------------------------------------------------------------

def _names(report):
    return [c["name"] for c in report.checks]


def _cmd(report, name):
    c = next(c for c in report.checks if c["name"] == name)
    return " ".join([c["cmd"], *c["args"]])


@pytest.fixture
def go_repo(tmp_path):
    repo = tmp_path / "ledger"
    (repo / "internal").mkdir(parents=True)
    (repo / "go.mod").write_text("module example.com/ledger\n\ngo 1.22\n")
    (repo / "internal" / "sum_test.go").write_text(
        "package internal\n\nfunc TestSum(t *testing.T) { _ = 1 + 1 }\n")
    _git_init(repo)
    return repo


def test_go_repo_gets_real_commands_not_just_a_language_label(go_repo):
    r = prov.detect_project(str(go_repo))
    assert r.languages == ["go"]
    assert _names(r) == ["vet", "build", "test"]
    assert _cmd(r, "test") == "go test ./..."
    assert _cmd(r, "vet") == "go vet ./..."
    assert r.risky_scripts == []
    assert {"dir": ".", "cmd": "go", "args": ["build", "./..."]} in r.build_steps


def test_golangci_lint_is_proposed_only_when_the_repo_configures_it(go_repo):
    assert "lint" not in _names(prov.detect_project(str(go_repo)))
    (go_repo / ".golangci.yml").write_text("linters:\n  enable: [govet]\n")
    r = prov.detect_project(str(go_repo))
    assert _cmd(r, "lint") == "golangci-lint run"


def test_go_suite_that_calls_the_network_is_not_a_check(tmp_path):
    """The large-project rule, applied where there are no script names to flag:
    the unit of suspicion is the suite, because `go test ./...` is."""
    repo = tmp_path / "gotrader"
    repo.mkdir()
    (repo / "go.mod").write_text("module example.com/gotrader\n")
    (repo / "trade_test.go").write_text(
        'package main\n\nimport "net/http"\n\n'
        'func TestOpen(t *testing.T) { http.Post("https://a-live-service/trade/open", "", nil) }\n')
    _git_init(repo)

    r = prov.detect_project(str(repo))
    assert "test" not in _names(r), "a suite that POSTs at a live service is not auto-enabled"
    flagged = {c.value: c for c in r.risky_scripts}
    assert flagged["test"].enabled is False
    assert flagged["test"].warning
    assert "trade_test.go" in flagged["test"].reason
    assert flagged["test"].check["args"] == ["test", "./..."], \
        "the candidate carries the exact command the operator would be enabling"
    assert any("network calls" in w for w in r.warnings)


def test_pure_go_tests_are_not_flagged(go_repo):
    r = prov.detect_project(str(go_repo))
    assert "test" in _names(r)
    assert r.risky_scripts == []


def test_a_makefile_review_target_is_the_cross_language_test_review(tmp_path):
    repo = tmp_path / "curated-go"
    repo.mkdir()
    (repo / "go.mod").write_text("module example.com/curated\n")
    (repo / "live_test.go").write_text(
        'package main\nimport "net/http"\nfunc TestLive(t *testing.T){ http.Get("https://prod/x") }\n')
    (repo / "Makefile").write_text(
        ".PHONY: test test-review\ntest:\n\tgo test ./...\ntest-review:\n\tgo test -short ./...\n")
    _git_init(repo)

    r = prov.detect_project(str(repo))
    assert _cmd(r, "test") == "make test-review", "the repo's own safe list wins"
    # the unguarded suite stays on offer, under a name of its own
    flagged = {c.value: c for c in r.risky_scripts}
    assert flagged["test-all"].enabled is False
    assert flagged["test-all"].check["args"] == ["test", "./..."]


def test_rust_checks_follow_the_repo_s_own_configuration(tmp_path):
    repo = tmp_path / "crate"
    (repo / "src").mkdir(parents=True)
    (repo / "Cargo.toml").write_text("[package]\nname = 'crate'\n")
    (repo / "src" / "lib.rs").write_text("pub fn a() {}\n\n#[test]\nfn t() { assert!(true); }\n")
    _git_init(repo)

    r = prov.detect_project(str(repo))
    assert r.languages == ["rust"]
    assert _names(r) == ["build", "test"], "no rustfmt.toml and no clippy.toml means neither is proposed"
    assert _cmd(r, "test") == "cargo test"
    assert {"dir": ".", "cmd": "cargo", "args": ["build", "--release"]} in r.build_steps

    (repo / "rustfmt.toml").write_text("edition = '2021'\n")
    (repo / "clippy.toml").write_text("msrv = '1.70'\n")
    r = prov.detect_project(str(repo))
    assert _cmd(r, "fmt") == "cargo fmt -- --check"
    # without -D warnings clippy exits 0 on everything it reports
    assert _cmd(r, "lint") == "cargo clippy --all-targets -- -D warnings"


def test_rust_source_without_a_test_attribute_is_not_scanned_as_a_test(tmp_path):
    """*.rs is the whole crate, not its tests. A client module that calls out
    is the code under review, not a suite that acts on production."""
    repo = tmp_path / "client-crate"
    (repo / "src").mkdir(parents=True)
    (repo / "Cargo.toml").write_text("[package]\nname = 'c'\n")
    (repo / "src" / "http.rs").write_text(
        "pub fn get() { reqwest::blocking::get(\"https://api.example.com\").unwrap(); }\n")
    _git_init(repo)
    r = prov.detect_project(str(repo))
    assert "test" in _names(r) and r.risky_scripts == []

    (repo / "tests").mkdir()
    (repo / "tests" / "live.rs").write_text(
        "#[test]\nfn hits() { reqwest::blocking::get(\"https://prod/x\").unwrap(); }\n")
    r = prov.detect_project(str(repo))
    assert "test" not in _names(r)
    assert [c.value for c in r.risky_scripts] == ["test"]


def test_cargo_alias_is_rust_s_declared_review_suite(tmp_path):
    repo = tmp_path / "aliased"
    (repo / ".cargo").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "Cargo.toml").write_text("[package]\nname = 'a'\n")
    (repo / ".cargo" / "config.toml").write_text(
        "[alias]\ntest-review = \"test --lib\"\n")
    (repo / "tests" / "live.rs").write_text(
        "#[test]\nfn hits() { reqwest::get(\"https://prod\"); }\n")
    _git_init(repo)
    r = prov.detect_project(str(repo))
    assert _cmd(r, "test") == "cargo test-review"
    assert [c.value for c in r.risky_scripts] == ["test-all"]


def test_ruby_rspec_project(tmp_path):
    repo = tmp_path / "rubyapp"
    (repo / "spec").mkdir(parents=True)
    (repo / "Gemfile").write_text("source 'https://rubygems.org'\ngem 'rspec'\n")
    (repo / "Gemfile.lock").write_text("")
    (repo / ".rubocop.yml").write_text("AllCops:\n  NewCops: enable\n")
    (repo / "spec" / "calc_spec.rb").write_text("describe('calc') { expect(1 + 1).to eq 2 }\n")
    _git_init(repo)

    r = prov.detect_project(str(repo))
    assert "ruby" in r.languages
    assert _cmd(r, "lint") == "bundle exec rubocop"
    assert _cmd(r, "test") == "bundle exec rspec"
    assert {"dir": ".", "cmd": "bundle", "args": ["install"]} in r.build_steps


def test_ruby_minitest_project_uses_rake(tmp_path):
    repo = tmp_path / "rakeapp"
    (repo / "test").mkdir(parents=True)
    (repo / "Gemfile").write_text("source 'https://rubygems.org'\n")
    (repo / "Rakefile").write_text("task :test do\nend\n")
    (repo / "test" / "calc_test.rb").write_text("assert_equal 2, 1 + 1\n")
    _git_init(repo)
    r = prov.detect_project(str(repo))
    assert _cmd(r, "test") == "bundle exec rake test"


def test_ruby_without_a_gemfile_does_not_pretend_to_have_bundler(tmp_path):
    repo = tmp_path / "plainruby"
    (repo / "spec").mkdir(parents=True)
    (repo / ".ruby-version").write_text("3.3.0\n")
    (repo / "spec" / "x_spec.rb").write_text("describe('x') { }\n")
    _git_init(repo)
    r = prov.detect_project(str(repo))
    assert _cmd(r, "test") == "rspec"


def test_ruby_rake_review_task_wins_over_the_full_suite(tmp_path):
    repo = tmp_path / "curated-ruby"
    (repo / "spec").mkdir(parents=True)
    (repo / "Gemfile").write_text("source 'https://rubygems.org'\n")
    (repo / "Rakefile").write_text("namespace :test do\n  task :review do\n  end\nend\n")
    (repo / "spec" / "live_spec.rb").write_text(
        "describe('live') { Net::HTTP.get(URI('https://prod/x')) }\n")
    _git_init(repo)
    r = prov.detect_project(str(repo))
    assert _cmd(r, "test") == "bundle exec rake test:review"
    assert [c.value for c in r.risky_scripts] == ["test-all"]


def test_makefile_is_the_fallback_for_a_repo_with_no_manifest(tmp_path):
    repo = tmp_path / "shellproj"
    repo.mkdir()
    (repo / "Makefile").write_text("test:\n\t./run-tests.sh\nlint:\n\tshellcheck *.sh\n")
    _git_init(repo)
    r = prov.detect_project(str(repo))
    assert r.languages == []
    assert _names(r) == ["lint", "test"]
    assert _cmd(r, "test") == "make test"
    assert any("Makefile targets" in w and "network calls" in w for w in r.warnings), \
        "a target cannot be read the way a test file can -- say so rather than implying it was checked"


def test_makefile_without_a_test_target_proposes_no_test_check(tmp_path):
    repo = tmp_path / "nomaketest"
    repo.mkdir()
    (repo / "Makefile").write_text("build:\n\tgcc -o x x.c\n")
    _git_init(repo)
    r = prov.detect_project(str(repo))
    assert _names(r) == ["build"]
    assert any("no automated checks" not in w for w in r.warnings)


def test_makefile_is_not_consulted_when_a_real_stack_was_detected(tmp_path):
    """A Go repo whose Makefile wraps the same commands must not get both."""
    repo = tmp_path / "gomake"
    repo.mkdir()
    (repo / "go.mod").write_text("module example.com/gm\n")
    (repo / "Makefile").write_text("test:\n\tgo test ./...\nlint:\n\tgolangci-lint run\n")
    _git_init(repo)
    r = prov.detect_project(str(repo))
    assert _cmd(r, "test") == "go test ./...", "the stack's own command, not the wrapper"
    assert not any(c["cmd"] == "make" for c in r.checks)


def test_a_polyglot_repo_keeps_both_stacks_with_stable_names(tmp_path):
    repo = tmp_path / "poly"
    repo.mkdir()
    (repo / "package.json").write_text(json.dumps({"scripts": {"lint": "eslint .", "test": "vitest run"}}))
    (repo / "package-lock.json").write_text("{}")
    (repo / "go.mod").write_text("module example.com/poly\n")
    (repo / "main_test.go").write_text("package main\n\nfunc TestX(t *testing.T) {}\n")
    _git_init(repo)

    r = prov.detect_project(str(repo))
    assert r.languages == ["node", "go"]
    assert _names(r) == ["lint", "test", "go-vet", "go-build", "go-test"], \
        "the second stack is prefixed wholesale, so no name is silently replaced"
    assert _cmd(r, "test") == "npm run test"
    assert _cmd(r, "go-test") == "go test ./..."


def test_a_polyglot_flagged_suite_is_renamed_with_its_check(tmp_path):
    """A candidate's name is how validate_choices finds the command it stands
    for; if the check is renamed and the candidate is not, enabling it would
    resolve to the wrong stack's suite."""
    repo = tmp_path / "poly-net"
    repo.mkdir()
    (repo / "package.json").write_text(json.dumps({"scripts": {"test": "vitest run"}}))
    (repo / "package-lock.json").write_text("{}")
    (repo / "go.mod").write_text("module example.com/pn\n")
    (repo / "live_test.go").write_text(
        'package main\nimport "net/http"\nfunc TestL(t *testing.T){ http.Get("https://prod/x") }\n')
    _git_init(repo)

    r = prov.detect_project(str(repo))
    flagged = {c.value: c for c in r.risky_scripts}
    assert "go-test" in flagged
    assert flagged["go-test"].check["name"] == "go-test"
    assert flagged["go-test"].check["args"] == ["test", "./..."]

    clean = prov.validate_choices(r, {"checks": [{"name": "go-test"}]})
    assert clean["checks"][0]["cmd"] == "go"
    assert clean["checks"][0]["args"] == ["test", "./..."]


def test_enabling_a_flagged_suite_runs_our_command_not_the_client_s(tmp_path):
    """The wizard is an approval step. A client that echoes back a different
    cmd/args under a proposed name gets the server's version regardless --
    the review service executes these verbatim."""
    repo = tmp_path / "gonet2"
    repo.mkdir()
    (repo / "go.mod").write_text("module example.com/g2\n")
    (repo / "x_test.go").write_text(
        'package main\nimport "net/http"\nfunc TestX(t *testing.T){ http.Get("https://prod") }\n')
    _git_init(repo)
    r = prov.detect_project(str(repo))

    clean = prov.validate_choices(r, {"checks": [
        {"name": "test", "dir": ".", "cmd": "curl", "args": ["https://attacker/x"], "timeoutMs": 1},
    ]})
    assert clean["checks"] == [{"name": "test", "dir": ".", "cmd": "go",
                                "args": ["test", "./..."],
                                "timeoutMs": prov.TEST_TIMEOUT_MS_DEFAULT}]

    with pytest.raises(prov.ProvisioningError):
        prov.validate_choices(r, {"checks": [{"name": "test-all"}]})


def test_python_suite_calling_the_network_is_flagged_too(tmp_path):
    """pytest has no per-suite script names either, so it gets the same rule
    the npm path has had since the npm-script incident."""
    repo = tmp_path / "pynet"
    (repo / "tests").mkdir(parents=True)
    (repo / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    (repo / "tests" / "test_live.py").write_text(
        "import requests\n\ndef test_live():\n    requests.post('https://prod/orders')\n")
    _git_init(repo)
    r = prov.detect_project(str(repo))
    assert "test" not in _names(r)
    assert [c.value for c in r.risky_scripts] == ["test"]
    assert r.risky_scripts[0].check["args"] == ["-m", "pytest", "-q"]


def test_vendored_and_ignored_directories_are_not_scanned(tmp_path):
    """A networked test inside vendor/ or node_modules/ is someone else's
    code. Flagging the repo's own suite for it would train the operator to
    enable flagged suites without reading them."""
    repo = tmp_path / "vendored"
    (repo / "vendor" / "dep").mkdir(parents=True)
    (repo / "node_modules" / "pkg").mkdir(parents=True)
    (repo / "go.mod").write_text("module example.com/v\n")
    (repo / "own_test.go").write_text("package main\n\nfunc TestOwn(t *testing.T) {}\n")
    (repo / "vendor" / "dep" / "dep_test.go").write_text(
        'package dep\nimport "net/http"\nfunc TestD(t *testing.T){ http.Get("https://x") }\n')
    (repo / "node_modules" / "pkg" / "pkg_test.go").write_text(
        'package pkg\nimport "net/http"\nfunc TestP(t *testing.T){ http.Post("https://y", "", nil) }\n')
    _git_init(repo)
    r = prov.detect_project(str(repo))
    assert "test" in _names(r)
    assert r.risky_scripts == []


def test_the_suite_scan_is_bounded(tmp_path, monkeypatch):
    """This runs inside a wizard click. A monorepo with ten thousand test
    files must not turn that into a minute of IO."""
    repo = tmp_path / "huge"
    repo.mkdir()
    (repo / "go.mod").write_text("module example.com/h\n")
    for i in range(30):
        (repo / f"a{i:03d}_test.go").write_text("package main\n")
    _git_init(repo)

    opened = []
    real_read = Path.read_text

    def counting_read(self, *a, **kw):
        opened.append(str(self))
        return real_read(self, *a, **kw)

    monkeypatch.setattr(prov, "_SCAN_FILE_LIMIT", 5)
    monkeypatch.setattr(Path, "read_text", counting_read)
    prov.detect_project(str(repo))
    assert len([p for p in opened if p.endswith("_test.go")]) <= 5


def test_a_missing_toolchain_is_a_warning_not_a_silent_failure(go_repo, monkeypatch):
    """The review service runs checks on this host. A missing `go` means every
    review of this project fails, forever, for a reason nothing else states."""
    monkeypatch.setattr(prov.shutil, "which", lambda c: None if c == "go" else f"/usr/bin/{c}")
    r = prov.detect_project(str(go_repo))
    assert any("go not on PATH" in w for w in r.warnings)

    monkeypatch.setattr(prov.shutil, "which", lambda c: f"/usr/bin/{c}")
    r = prov.detect_project(str(go_repo))
    assert not any("not on PATH" in w for w in r.warnings)


# ---------------------------------------------------------------------------
# False positives are a real cost, not a free safety margin
#
# The network rule is deliberately suspicious, and a wizard that flags every
# honest suite trains the operator to click through the flags -- the same
# failure as not flagging at all, reached more slowly. These hold the line
# between "we cannot prove this is safe" and "this obviously goes nowhere".
# ---------------------------------------------------------------------------

def test_a_go_test_driving_httptest_is_not_flagged(tmp_path):
    """httptest.NewServer IS the local server the http.Get is aimed at."""
    repo = tmp_path / "httptest-go"
    repo.mkdir()
    (repo / "go.mod").write_text("module example.com/ht\n")
    (repo / "api_test.go").write_text(
        'package main\n\nimport (\n\t"net/http"\n\t"net/http/httptest"\n)\n\n'
        'func TestAPI(t *testing.T) {\n'
        '\tsrv := httptest.NewServer(handler())\n'
        '\tdefer srv.Close()\n'
        '\thttp.Get(srv.URL + "/health")\n}\n')
    _git_init(repo)
    r = prov.detect_project(str(repo))
    assert "test" in _names(r), "a test server the suite starts itself is not production"
    assert r.risky_scripts == []


def test_a_ruby_spec_with_webmock_is_not_flagged(tmp_path):
    """WebMock refuses real connections by default -- that is its purpose."""
    repo = tmp_path / "webmocked"
    (repo / "spec").mkdir(parents=True)
    (repo / "Gemfile").write_text("source 'https://rubygems.org'\ngem 'webmock'\n")
    (repo / "spec" / "client_spec.rb").write_text(
        "require 'webmock/rspec'\n\n"
        "describe Client do\n"
        "  it 'fetches' do\n"
        "    stub_request(:get, 'https://api.example.com/x')\n"
        "    Net::HTTP.get(URI.parse('https://api.example.com/x'))\n"
        "  end\n"
        "end\n")
    _git_init(repo)
    r = prov.detect_project(str(repo))
    assert "test" in _names(r)
    assert r.risky_scripts == []


def test_uri_parse_alone_is_not_a_network_call(tmp_path):
    """Parsing a string opens nothing. It appeared in every spec that builds
    a URL for a stubbed request, which is most of them."""
    repo = tmp_path / "uriparse"
    (repo / "spec").mkdir(parents=True)
    (repo / "Gemfile").write_text("source 'https://rubygems.org'\n")
    (repo / "spec" / "url_spec.rb").write_text(
        "describe 'urls' do\n  it 'parses' do\n"
        "    expect(URI.parse('https://example.com/a').host).to eq 'example.com'\n"
        "  end\nend\n")
    _git_init(repo)
    r = prov.detect_project(str(repo))
    assert "test" in _names(r)
    assert r.risky_scripts == []


def test_a_python_test_using_responses_is_not_flagged(tmp_path):
    repo = tmp_path / "responses-py"
    (repo / "tests").mkdir(parents=True)
    (repo / "pyproject.toml").write_text("[project]\nname = 'r'\n")
    (repo / "tests" / "test_client.py").write_text(
        "import responses\nimport requests\n\n"
        "@responses.activate\ndef test_get():\n"
        "    responses.add(responses.GET, 'https://api.example.com/x', json={})\n"
        "    requests.get('https://api.example.com/x')\n")
    _git_init(repo)
    r = prov.detect_project(str(repo))
    assert "test" in _names(r)
    assert r.risky_scripts == []


def test_stubbing_is_judged_per_file_not_per_repo(tmp_path):
    """One spec wired to WebMock says nothing about the smoke test three
    directories over."""
    repo = tmp_path / "mixed"
    (repo / "spec").mkdir(parents=True)
    (repo / "Gemfile").write_text("source 'https://rubygems.org'\n")
    (repo / "spec" / "stubbed_spec.rb").write_text(
        "require 'webmock/rspec'\nstub_request(:get, 'https://x/')\nNet::HTTP.get(URI('https://x/'))\n")
    (repo / "spec" / "smoke_spec.rb").write_text(
        "describe 'smoke' do\n  it 'hits prod' do\n"
        "    Net::HTTP.post(URI('https://payments.example.com/charge'), '')\n"
        "  end\nend\n")
    _git_init(repo)
    r = prov.detect_project(str(repo))
    assert "test" not in _names(r)
    reason = r.risky_scripts[0].reason
    assert "smoke_spec.rb" in reason and "stubbed_spec.rb" not in reason


def test_an_unstubbed_localhost_call_is_still_flagged(tmp_path):
    """The incident this rule exists for hit a service on localhost. Local is
    not the same as harmless -- this deployment's own bot ran there."""
    repo = tmp_path / "localhost-go"
    repo.mkdir()
    (repo / "go.mod").write_text("module example.com/lh\n")
    (repo / "bot_test.go").write_text(
        'package main\nimport "net/http"\n'
        'func TestTrade(t *testing.T){ http.Post("http://127.0.0.1:8080/trade/open", "", nil) }\n')
    _git_init(repo)
    r = prov.detect_project(str(repo))
    assert "test" not in _names(r)
    assert [c.value for c in r.risky_scripts] == ["test"]


# ---------------------------------------------------------------------------
# Rake task detection reads a block, not the whole file
# ---------------------------------------------------------------------------

def _ruby_repo(tmp_path, name, rakefile):
    repo = tmp_path / name
    (repo / "spec").mkdir(parents=True)
    (repo / "Gemfile").write_text("source 'https://rubygems.org'\n")
    (repo / "Rakefile").write_text(rakefile)
    (repo / "spec" / "live_spec.rb").write_text(
        "describe('live') { Net::HTTP.get(URI('https://prod/x')) }\n")
    _git_init(repo)
    return repo


def test_review_task_under_an_unrelated_namespace_is_not_test_review(tmp_path):
    """`namespace :test` at the top and `task :review` under `namespace
    :deploy` further down is not a declaration of test:review, and handing
    the review gate that task would run something else entirely."""
    repo = _ruby_repo(tmp_path, "misleading", (
        "namespace :test do\n"
        "  task :unit do\n  end\n"
        "end\n"
        "\n"
        "namespace :deploy do\n"
        "  task :review do\n  end\n"
        "end\n"))
    r = prov.detect_project(str(repo))
    assert "test" not in _names(r), "no curated suite was declared, and the specs call out"
    assert [c.value for c in r.risky_scripts] == ["test"]


def test_review_task_inside_the_test_namespace_is_found(tmp_path):
    repo = _ruby_repo(tmp_path, "genuine", (
        "namespace :test do\n"
        "  desc 'suites safe for an outside reviewer'\n"
        "  task :review do\n  end\n"
        "end\n"))
    r = prov.detect_project(str(repo))
    assert _cmd(r, "test") == "bundle exec rake test:review"


def test_the_flat_spelling_still_counts(tmp_path):
    repo = _ruby_repo(tmp_path, "flat", "task 'test:review' do\nend\n")
    r = prov.detect_project(str(repo))
    assert _cmd(r, "test") == "bundle exec rake test:review"


def test_a_bare_mention_of_test_review_in_a_comment_is_not_a_declaration(tmp_path):
    repo = _ruby_repo(tmp_path, "commented", "# TODO: add a test:review task one day\n")
    r = prov.detect_project(str(repo))
    assert "test" not in _names(r)


# ---------------------------------------------------------------------------
# bundler: one predicate for the checks and the deploy
# ---------------------------------------------------------------------------

def test_a_gemfile_without_a_lockfile_still_gets_bundle_install(tmp_path):
    """Keying the deploy step on Gemfile.lock left a library repo running
    `bundle exec rspec` in review against a bundle the deploy never
    installed."""
    repo = tmp_path / "libgem"
    (repo / "spec").mkdir(parents=True)
    (repo / "Gemfile").write_text("source 'https://rubygems.org'\ngem 'rspec'\n")
    (repo / "spec" / "x_spec.rb").write_text("describe('x') { }\n")
    _git_init(repo)
    r = prov.detect_project(str(repo))
    assert _cmd(r, "test") == "bundle exec rspec"
    assert {"dir": ".", "cmd": "bundle", "args": ["install"]} in r.build_steps


def test_a_repo_with_no_bundler_gets_neither(tmp_path):
    repo = tmp_path / "nobundler"
    (repo / "spec").mkdir(parents=True)
    (repo / ".ruby-version").write_text("3.3.0\n")
    (repo / "spec" / "x_spec.rb").write_text("describe('x') { }\n")
    _git_init(repo)
    r = prov.detect_project(str(repo))
    assert _cmd(r, "test") == "rspec"
    assert r.build_steps == []


# ---------------------------------------------------------------------------
# Elixir, Java, PHP and .NET
#
# The last four stacks that onboarded with an empty checks list. Same three
# rules as everywhere else: the repo's own commands, a suite that calls the
# network arrives disabled, and the client may only narrow what was proposed.
# scripts/verify_stack_checks.py runs every command below in the official
# toolchain image -- that is where the spelling is proven, not here.
# ---------------------------------------------------------------------------

def test_elixir_project_gets_mix_commands(tmp_path):
    repo = tmp_path / "elixirapp"
    (repo / "test").mkdir(parents=True)
    (repo / "mix.exs").write_text("defmodule App.MixProject do\n  def project, do: [app: :app]\nend\n")
    (repo / "test" / "calc_test.exs").write_text(
        "defmodule CalcTest do\n  use ExUnit.Case\n  test \"adds\" do\n    assert 1 + 1 == 2\n  end\nend\n")
    _git_init(repo)

    r = prov.detect_project(str(repo))
    assert r.languages == ["elixir"]
    assert _names(r) == ["test"], "neither .formatter.exs nor .credo.exs means neither is proposed"
    assert _cmd(r, "test") == "mix test"
    assert {"dir": ".", "cmd": "mix", "args": ["deps.get"]} in r.build_steps

    (repo / ".formatter.exs").write_text('[inputs: ["lib/**/*.ex"]]\n')
    (repo / ".credo.exs").write_text("%{configs: []}\n")
    r = prov.detect_project(str(repo))
    assert _cmd(r, "format") == "mix format --check-formatted"
    assert _cmd(r, "lint") == "mix credo --strict"


def test_a_mix_alias_is_elixirs_declared_review_suite(tmp_path):
    repo = tmp_path / "curated-elixir"
    (repo / "test").mkdir(parents=True)
    (repo / "mix.exs").write_text(
        'defmodule App.MixProject do\n'
        '  defp aliases, do: ["test.review": ["test --only safe"]]\n'
        'end\n')
    (repo / "test" / "live_test.exs").write_text(
        'defmodule LiveTest do\n  test "hits" do\n    HTTPoison.get!("https://prod/x")\n  end\nend\n')
    _git_init(repo)
    r = prov.detect_project(str(repo))
    assert _cmd(r, "test") == "mix test.review"
    assert [c.value for c in r.risky_scripts] == ["test-all"]


def test_an_elixir_test_using_bypass_is_not_flagged(tmp_path):
    """Bypass is an in-process HTTP server the test itself starts."""
    repo = tmp_path / "bypassed"
    (repo / "test").mkdir(parents=True)
    (repo / "mix.exs").write_text("defmodule App.MixProject do\nend\n")
    (repo / "test" / "client_test.exs").write_text(
        'defmodule ClientTest do\n  setup do\n    bypass = Bypass.open()\n    {:ok, bypass: bypass}\n  end\n'
        '  test "fetches", %{bypass: bypass} do\n    HTTPoison.get!("http://localhost:#{bypass.port}/x")\n  end\nend\n')
    _git_init(repo)
    r = prov.detect_project(str(repo))
    assert "test" in _names(r) and r.risky_scripts == []


def test_a_maven_project_runs_in_batch_mode(tmp_path):
    repo = tmp_path / "mavensvc"
    (repo / "src" / "test" / "java").mkdir(parents=True)
    (repo / "pom.xml").write_text("<project><artifactId>svc</artifactId></project>\n")
    (repo / "src" / "test" / "java" / "CalcTest.java").write_text("class CalcTest { void t() {} }\n")
    _git_init(repo)
    r = prov.detect_project(str(repo))
    assert r.languages == ["java"]
    # -B, because the reviewer has no terminal and Maven otherwise fills the
    # captured output with progress bars
    assert _cmd(r, "test") == "mvn -B test"
    assert _cmd(r, "build") == "mvn -B -DskipTests package"


def test_a_gradle_project_prefers_the_wrapper_the_repo_ships(tmp_path):
    """A repo with a gradlew is pinning a Gradle version on purpose; running
    the host's own `gradle` is how a build works for the author and not for
    the reviewer."""
    repo = tmp_path / "gradlesvc"
    (repo / "src" / "test" / "java").mkdir(parents=True)
    (repo / "build.gradle").write_text("plugins { id 'java' }\n")
    (repo / "src" / "test" / "java" / "CalcTest.java").write_text("class CalcTest { void t() {} }\n")
    _git_init(repo)
    assert _cmd(prov.detect_project(str(repo)), "test") == "gradle test"

    (repo / "gradlew").write_text("#!/bin/sh\n")
    (repo / "gradlew").chmod(0o755)
    assert _cmd(prov.detect_project(str(repo)), "test") == "./gradlew test"


def test_a_gradle_test_review_task_is_preferred(tmp_path):
    repo = tmp_path / "curated-gradle"
    (repo / "src" / "test" / "java").mkdir(parents=True)
    (repo / "build.gradle").write_text("plugins { id 'java' }\ntask testReview(type: Test) { }\n")
    (repo / "src" / "test" / "java" / "ApiTest.java").write_text(
        'class ApiTest { void t() { new java.net.URL("https://prod/x").openStream(); } }\n')
    _git_init(repo)
    r = prov.detect_project(str(repo))
    assert _cmd(r, "test") == "gradle testReview"
    assert [c.value for c in r.risky_scripts] == ["test-all"]


def test_a_jvm_test_using_wiremock_is_not_flagged(tmp_path):
    repo = tmp_path / "wiremocked"
    (repo / "src" / "test" / "java").mkdir(parents=True)
    (repo / "pom.xml").write_text("<project><artifactId>x</artifactId></project>\n")
    (repo / "src" / "test" / "java" / "ApiTest.java").write_text(
        'import com.github.tomakehurst.wiremock.WireMockServer;\n'
        'class ApiTest { void t() { new java.net.URL("http://localhost:8089/x").openStream(); } }\n')
    _git_init(repo)
    r = prov.detect_project(str(repo))
    assert "test" in _names(r) and r.risky_scripts == []


def test_php_prefers_the_repos_own_composer_script(tmp_path):
    repo = tmp_path / "phpapp"
    (repo / "tests").mkdir(parents=True)
    (repo / "composer.json").write_text(json.dumps({"scripts": {"test": "phpunit"}}))
    (repo / "tests" / "CalcTest.php").write_text("<?php class CalcTest { function testAdds() {} }\n")
    _git_init(repo)
    r = prov.detect_project(str(repo))
    assert r.languages == ["php"]
    # run-script, never the bare shorthand: a script named like a composer
    # subcommand would otherwise run composer's own command
    assert _cmd(r, "test") == "composer run-script test"
    assert {"dir": ".", "cmd": "composer",
            "args": ["install", "--no-interaction", "--no-progress"]} in r.build_steps


def test_php_falls_back_to_phpunit_when_no_script_is_declared(tmp_path):
    repo = tmp_path / "phpbare"
    (repo / "tests").mkdir(parents=True)
    (repo / "composer.json").write_text(json.dumps({"require": {"php": "^8.2"}}))
    (repo / "phpunit.xml").write_text("<phpunit/>\n")
    (repo / "tests" / "CalcTest.php").write_text("<?php class CalcTest {}\n")
    _git_init(repo)
    assert _cmd(prov.detect_project(str(repo)), "test") == "vendor/bin/phpunit"


def test_php_static_analysis_is_proposed_only_when_configured(tmp_path):
    repo = tmp_path / "phpstan-app"
    (repo / "tests").mkdir(parents=True)
    (repo / "composer.json").write_text(json.dumps({"scripts": {"test": "phpunit"}}))
    (repo / "tests" / "CalcTest.php").write_text("<?php class CalcTest {}\n")
    _git_init(repo)
    assert "analyse" not in _names(prov.detect_project(str(repo)))

    (repo / "phpstan.neon").write_text("parameters:\n  level: 5\n")
    (repo / "phpcs.xml").write_text("<ruleset/>\n")
    r = prov.detect_project(str(repo))
    assert _cmd(r, "analyse") == "vendor/bin/phpstan analyse --no-progress"
    assert _cmd(r, "lint") == "vendor/bin/phpcs -q"


def test_a_php_test_hitting_a_live_host_is_flagged(tmp_path):
    repo = tmp_path / "phpnet"
    (repo / "tests").mkdir(parents=True)
    (repo / "composer.json").write_text(json.dumps({"scripts": {"test": "phpunit"}}))
    (repo / "tests" / "PayTest.php").write_text(
        "<?php\nclass PayTest {\n  function testCharges() {\n"
        "    (new GuzzleHttp\\Client())->post('https://payments.example.com/charge');\n  }\n}\n")
    _git_init(repo)
    r = prov.detect_project(str(repo))
    assert "test" not in _names(r)
    assert r.risky_scripts[0].check["args"] == ["run-script", "test"]


def test_a_php_test_using_http_fake_is_not_flagged(tmp_path):
    repo = tmp_path / "phpfake"
    (repo / "tests").mkdir(parents=True)
    (repo / "composer.json").write_text(json.dumps({"scripts": {"test": "phpunit"}}))
    (repo / "tests" / "PayTest.php").write_text(
        "<?php\nclass PayTest {\n  function testCharges() {\n"
        "    Http::fake();\n    Http::post('https://payments.example.com/charge');\n  }\n}\n")
    _git_init(repo)
    r = prov.detect_project(str(repo))
    assert "test" in _names(r) and r.risky_scripts == []


def test_a_dotnet_solution_is_detected_from_the_root_or_one_level_down(tmp_path):
    repo = tmp_path / "netapp"
    (repo / "src" / "App").mkdir(parents=True)
    (repo / "src" / "App" / "App.csproj").write_text('<Project Sdk="Microsoft.NET.Sdk"></Project>\n')
    _git_init(repo)
    r = prov.detect_project(str(repo))
    assert r.languages == ["dotnet"]
    assert _cmd(r, "test") == "dotnet test --nologo"
    assert _cmd(r, "build") == "dotnet build --nologo"
    assert {"dir": ".", "cmd": "dotnet",
            "args": ["build", "--nologo", "-c", "Release"]} in r.build_steps


def test_dotnet_format_needs_an_editorconfig(tmp_path):
    """`dotnet format` reads .editorconfig and nothing else; without one it
    would enforce defaults the repo never chose."""
    repo = tmp_path / "netfmt"
    repo.mkdir()
    (repo / "App.sln").write_text("Microsoft Visual Studio Solution File\n")
    _git_init(repo)
    assert "format" not in _names(prov.detect_project(str(repo)))
    (repo / ".editorconfig").write_text("root = true\n")
    assert _cmd(prov.detect_project(str(repo)), "format") == "dotnet format --verify-no-changes"


def test_a_dotnet_test_calling_out_is_flagged_and_moq_is_not(tmp_path):
    repo = tmp_path / "nettests"
    repo.mkdir()
    (repo / "App.csproj").write_text('<Project Sdk="Microsoft.NET.Sdk"></Project>\n')
    (repo / "PayTests.cs").write_text(
        "public class PayTests {\n  public async Task Charges() {\n"
        "    using var c = new HttpClient();\n"
        "    await c.PostAsync(\"https://payments.example.com/charge\", null);\n  }\n}\n")
    _git_init(repo)
    r = prov.detect_project(str(repo))
    assert "test" not in _names(r)

    (repo / "PayTests.cs").write_text(
        "using Moq;\npublic class PayTests {\n  public void Charges() {\n"
        "    var handler = new Mock<HttpMessageHandler>();\n"
        "    var c = new HttpClient(handler.Object);\n  }\n}\n")
    r = prov.detect_project(str(repo))
    assert "test" in _names(r) and r.risky_scripts == []


def test_every_supported_stack_is_reachable_from_detect_languages(tmp_path):
    """One fixture per stack, asserting detection fires at all. A stack whose
    trigger file stops matching produces an empty checks list, which is the
    silent failure this whole slice exists to remove."""
    fixtures = {
        "node": ("package.json", "{}"),
        "python": ("pyproject.toml", "[project]\nname='x'\n"),
        "go": ("go.mod", "module x\n"),
        "rust": ("Cargo.toml", "[package]\nname='x'\n"),
        "ruby": ("Gemfile", "source 'https://rubygems.org'\n"),
        "elixir": ("mix.exs", "defmodule X do\nend\n"),
        "java": ("pom.xml", "<project/>\n"),
        "php": ("composer.json", "{}"),
        "dotnet": ("App.csproj", "<Project/>\n"),
    }
    for lang, (fname, body) in fixtures.items():
        repo = tmp_path / f"probe-{lang}"
        repo.mkdir()
        (repo / fname).write_text(body)
        _git_init(repo)
        assert lang in prov.detect_project(str(repo)).languages, f"{fname} no longer means {lang}"


# ---------------------------------------------------------------------------
# Residuals found by onboarding throwaway repos rather than by reading
#
# Every case below was a real false positive or false negative on a fixture
# repo. They share one shape: a pattern that matched text rather than
# structure. A wizard that is wrong in the direction of "looks fine" is worse
# than one that is wrong loudly, because nobody goes back to check it.
# ---------------------------------------------------------------------------

def test_a_gradle_comment_is_not_a_declared_review_suite(tmp_path):
    """`// TODO: add testReview later` proposed `gradle testReview` as the
    repo's curated suite -- the same bare-word bug the rake path already
    had, on the stack that watched it get fixed."""
    repo = tmp_path / "gradle-comment"
    (repo / "src" / "test" / "java").mkdir(parents=True)
    (repo / "build.gradle").write_text(
        "plugins { id 'java' }\n\n// TODO: add testReview later\ntest { useJUnitPlatform() }\n")
    (repo / "src" / "test" / "java" / "ApiTest.java").write_text(
        'class ApiTest { void t() { new java.net.URL("https://prod/x").openStream(); } }\n')
    _git_init(repo)
    r = prov.detect_project(str(repo))
    assert "test" not in _names(r), "a comment must not become the review suite"
    assert [c.value for c in r.risky_scripts] == ["test"]


@pytest.mark.parametrize("declaration", [
    "task testReview(type: Test) { }",
    'tasks.register("testReview") { }',
    'tasks.register<Test>("testReview") { }',
    'tasks.create("testReview") { }',
    "val testReview by tasks.registering { }",
])
def test_every_spelling_of_a_gradle_task_declaration_counts(tmp_path, declaration):
    repo = tmp_path / f"gradle-{abs(hash(declaration))}"
    (repo / "src" / "test" / "java").mkdir(parents=True)
    (repo / "build.gradle").write_text(f"plugins {{ id 'java' }}\n{declaration}\n")
    (repo / "src" / "test" / "java" / "ApiTest.java").write_text("class ApiTest {}\n")
    _git_init(repo)
    assert _cmd(prov.detect_project(str(repo)), "test") == "gradle testReview"


def test_a_mix_alias_declared_as_a_string_counts(tmp_path):
    """`"test.review": "test --only safe"` is as valid as the list form.
    Requiring `[` meant the repo was told it had no curated suite, so its
    real one arrived flagged."""
    repo = tmp_path / "mix-string-alias"
    (repo / "test").mkdir(parents=True)
    (repo / "mix.exs").write_text(
        'defmodule App.MixProject do\n'
        '  defp aliases, do: ["test.review": "test --only safe"]\n'
        'end\n')
    (repo / "test" / "live_test.exs").write_text(
        'defmodule LiveTest do\n  test "hits" do\n    HTTPoison.get!("https://prod/x")\n  end\nend\n')
    _git_init(repo)
    r = prov.detect_project(str(repo))
    assert _cmd(r, "test") == "mix test.review"


def test_a_commented_out_mix_alias_does_not_count(tmp_path):
    repo = tmp_path / "mix-comment"
    (repo / "test").mkdir(parents=True)
    (repo / "mix.exs").write_text(
        'defmodule App.MixProject do\n  # "test.review": ["test --only safe"]\nend\n')
    (repo / "test" / "live_test.exs").write_text(
        'defmodule LiveTest do\n  test "x" do\n    HTTPoison.get!("https://prod/x")\n  end\nend\n')
    _git_init(repo)
    r = prov.detect_project(str(repo))
    assert "test" not in _names(r)


def test_a_hex_packages_own_tests_are_not_this_repos_tests(tmp_path):
    """deps/ is other people's code. A package's suite calling HTTPoison
    flagged the application's suite -- the same reason vendor/ and
    node_modules/ were already skipped."""
    repo = tmp_path / "elixir-deps"
    (repo / "test").mkdir(parents=True)
    (repo / "deps" / "httpoison" / "test").mkdir(parents=True)
    (repo / "_build" / "test").mkdir(parents=True)
    (repo / "mix.exs").write_text("defmodule App.MixProject do\nend\n")
    (repo / "test" / "calc_test.exs").write_text(
        'defmodule CalcTest do\n  test "adds" do\n    assert 1 + 1 == 2\n  end\nend\n')
    (repo / "deps" / "httpoison" / "test" / "client_test.exs").write_text(
        'defmodule ClientTest do\n  test "gets" do\n    HTTPoison.get!("https://example.com")\n  end\nend\n')
    (repo / "_build" / "test" / "stale_test.exs").write_text(
        'defmodule StaleTest do\n  test "x" do\n    HTTPoison.post!("https://prod/x", "")\n  end\nend\n')
    _git_init(repo)
    r = prov.detect_project(str(repo))
    assert "test" in _names(r), "a dependency's suite says nothing about this repo's"
    assert r.risky_scripts == []


def test_the_word_bypass_in_a_comment_does_not_neutralise_a_suite(tmp_path):
    """`# bypass the cache` next to a real HTTPoison call left the suite
    enabled. The stub list is library names; matching them case-insensitively
    matched English instead."""
    repo = tmp_path / "bypass-comment"
    (repo / "test").mkdir(parents=True)
    (repo / "mix.exs").write_text("defmodule App.MixProject do\nend\n")
    (repo / "test" / "live_test.exs").write_text(
        'defmodule LiveTest do\n'
        '  # bypass the cache so the numbers are fresh\n'
        '  test "charges" do\n    HTTPoison.post!("https://payments.example.com/charge", "")\n  end\n'
        'end\n')
    _git_init(repo)
    r = prov.detect_project(str(repo))
    assert "test" not in _names(r), "an English word in a comment is not a stub library"
    assert [c.value for c in r.risky_scripts] == ["test"]


def test_a_real_bypass_still_counts(tmp_path):
    repo = tmp_path / "bypass-real"
    (repo / "test").mkdir(parents=True)
    (repo / "mix.exs").write_text("defmodule App.MixProject do\nend\n")
    (repo / "test" / "client_test.exs").write_text(
        'defmodule ClientTest do\n  setup do\n    bypass = Bypass.open()\n    {:ok, bypass: bypass}\n  end\n'
        '  test "gets", %{bypass: b} do\n    HTTPoison.get!("http://localhost:#{b.port}/x")\n  end\nend\n')
    _git_init(repo)
    r = prov.detect_project(str(repo))
    assert "test" in _names(r) and r.risky_scripts == []


def test_a_repo_shipping_a_gradle_wrapper_is_not_told_gradle_is_missing(tmp_path, monkeypatch):
    """The whole point of a wrapper is that the host needs no Gradle, and
    `./gradlew` is never on PATH by construction."""
    monkeypatch.setattr(prov.shutil, "which", lambda c: None)
    repo = tmp_path / "wrapped"
    (repo / "src" / "test" / "java").mkdir(parents=True)
    (repo / "build.gradle").write_text("plugins { id 'java' }\n")
    (repo / "gradlew").write_text("#!/bin/sh\n")
    (repo / "gradlew").chmod(0o755)
    (repo / "src" / "test" / "java" / "CalcTest.java").write_text("class CalcTest {}\n")
    _git_init(repo)
    r = prov.detect_project(str(repo))
    assert _cmd(r, "test") == "./gradlew test"
    assert not any("not on PATH" in w for w in r.warnings), r.warnings


def test_a_binary_the_install_step_creates_is_not_reported_missing(tmp_path, monkeypatch):
    """vendor/bin/phpstan does not exist until `composer install` runs, and
    that install is the deploy's first build step."""
    monkeypatch.setattr(prov.shutil, "which", lambda c: None)
    repo = tmp_path / "phpstan-missing"
    (repo / "tests").mkdir(parents=True)
    (repo / "composer.json").write_text(json.dumps({"scripts": {"test": "phpunit"}}))
    (repo / "phpstan.neon").write_text("parameters:\n  level: 5\n")
    (repo / "tests" / "CalcTest.php").write_text("<?php class CalcTest {}\n")
    _git_init(repo)
    r = prov.detect_project(str(repo))
    assert _cmd(r, "analyse").startswith("vendor/bin/phpstan")
    # composer itself is a fair warning here -- it really is missing. phpstan
    # is not: `composer install` is the deploy's first build step.
    assert not any("phpstan" in w for w in r.warnings), r.warnings


def test_a_php_project_proposes_the_vendor_dir_the_reviewer_must_materialise(tmp_path):
    """`vendor/bin/phpunit` in a fresh worktree exits 127: vendor/ is
    gitignored, so nothing put it there. The reviewer binds whatever is
    listed here read-only from the live checkout."""
    repo = tmp_path / "php-vendor"
    (repo / "tests").mkdir(parents=True)
    (repo / "vendor" / "bin").mkdir(parents=True)
    (repo / "composer.json").write_text(json.dumps({"scripts": {"test": "phpunit"}}))
    (repo / "tests" / "CalcTest.php").write_text("<?php class CalcTest {}\n")
    _git_init(repo)
    r = prov.detect_project(str(repo))
    assert r.dependency_dirs == ["vendor"]

    clean = prov.validate_choices(r, {"dependency_dirs": ["vendor"]})
    entry = prov.config_from_choices(r.name, r.live, r.sandbox, clean)
    assert entry["review"]["dependencyDirs"] == ["vendor"]


def test_an_elixir_project_proposes_deps_but_never_build(tmp_path):
    """`_build` is compilation output, not dependencies, and `mix test`
    writes to it. The reviewer binds these read-only, so mounting _build
    would break every Elixir review -- and mounting it writable would let an
    unreviewed branch recompile over production's build."""
    repo = tmp_path / "elixir-dirs"
    (repo / "test").mkdir(parents=True)
    (repo / "deps").mkdir()
    (repo / "_build").mkdir()
    (repo / "mix.exs").write_text("defmodule App.MixProject do\nend\n")
    (repo / "test" / "calc_test.exs").write_text('defmodule CalcTest do\nend\n')
    _git_init(repo)
    assert prov.detect_project(str(repo)).dependency_dirs == ["deps"]


def test_ruby_proposes_vendor_bundle_only_when_the_project_bundles_into_itself(tmp_path):
    repo = tmp_path / "ruby-bundled"
    (repo / "spec").mkdir(parents=True)
    (repo / "Gemfile").write_text("source 'https://rubygems.org'\n")
    (repo / "spec" / "x_spec.rb").write_text("describe('x') { }\n")
    _git_init(repo)
    assert prov.detect_project(str(repo)).dependency_dirs == [], \
        "the default install is user-wide, and a worktree inherits it"

    (repo / "vendor" / "bundle").mkdir(parents=True)
    assert prov.detect_project(str(repo)).dependency_dirs == ["vendor/bundle"]


def test_a_js_monorepos_packages_directory_is_still_scanned(tmp_path):
    """`packages/` is .NET's old vendored dir and every JS monorepo's
    first-party source. Skipping it by name hid the repo's own tests."""
    repo = tmp_path / "monorepo"
    (repo / "packages" / "api").mkdir(parents=True)
    (repo / "pyproject.toml").write_text("[project]\nname='x'\n")
    (repo / "packages" / "api" / "test_live.py").write_text(
        "import requests\n\ndef test_live():\n    requests.post('https://prod/charge')\n")
    _git_init(repo)
    r = prov.detect_project(str(repo))
    assert "test" not in _names(r), "a networked test under packages/ must still be seen"


def test_a_stack_with_a_user_wide_cache_proposes_nothing(tmp_path):
    """Go, Rust, Maven, Gradle and NuGet all resolve from a cache outside the
    project, which a worktree inherits for free."""
    repo = tmp_path / "go-nodeps"
    repo.mkdir()
    (repo / "go.mod").write_text("module example.com/x\n")
    _git_init(repo)
    assert prov.detect_project(str(repo)).dependency_dirs == []


def test_a_dependency_dir_the_server_did_not_propose_is_refused(tmp_path):
    """The reviewer mounts these paths out of the live checkout, so they
    are a capability, not a preference."""
    repo = tmp_path / "php-narrow"
    (repo / "tests").mkdir(parents=True)
    (repo / "vendor").mkdir()
    (repo / "composer.json").write_text(json.dumps({"scripts": {"test": "phpunit"}}))
    _git_init(repo)
    r = prov.detect_project(str(repo))
    with pytest.raises(prov.ProvisioningError):
        prov.validate_choices(r, {"dependency_dirs": ["../../etc"]})
    with pytest.raises(prov.ProvisioningError):
        prov.validate_choices(r, {"dependency_dirs": [".git"]})


def test_a_repo_with_both_maven_and_gradle_says_which_one_was_used(tmp_path):
    repo = tmp_path / "dual-build"
    (repo / "src" / "test" / "java").mkdir(parents=True)
    (repo / "pom.xml").write_text("<project><artifactId>x</artifactId></project>\n")
    (repo / "build.gradle").write_text("plugins { id 'java' }\n")
    (repo / "src" / "test" / "java" / "CalcTest.java").write_text("class CalcTest {}\n")
    _git_init(repo)
    r = prov.detect_project(str(repo))
    assert _cmd(r, "test") == "mvn -B test"
    assert any("both pom.xml and a Gradle build file" in w for w in r.warnings)


def test_the_no_manifest_warning_lists_the_stacks_that_exist(tmp_path):
    repo = tmp_path / "bare"
    repo.mkdir()
    (repo / "hello.txt").write_text("hi\n")
    _git_init(repo)
    warning = next(w for w in prov.detect_project(str(repo)).warnings if "no recognized" in w)
    for manifest in ("mix.exs", "pom.xml", "composer.json", "csproj", "Gemfile"):
        assert manifest in warning, f"{manifest} is a stack we support and the warning omits it"
