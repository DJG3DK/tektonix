#!/usr/bin/env bash
# The installer's consent rules, checked without installing anything.
#
# The rule being protected is the one a person would be angriest about if it
# were wrong: nothing gets installed onto a machine without being asked, and
# --yes is not an answer to that question. An unattended run should mean "do
# not stop to ask me", never "put a Node runtime and a database on this host".
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1

TEKTONIX_INSTALL_LIB=1
# shellcheck source=../install.sh
source ./install.sh

ran=0; failed=0
it() {
    ran=$((ran + 1))
    if "$2" >/dev/null 2>&1; then printf '  ok   %s\n' "$1"
    else failed=$((failed + 1)); printf '  FAIL %s\n' "$1"; fi
}

# --- package names ----------------------------------------------------------

t_apt_docker()    { PKG_MGR=apt;    [ "$(prereq_package docker)" = "docker.io" ]; }
t_pacman_docker() { PKG_MGR=pacman; [ "$(prereq_package docker)" = "docker" ]; }
t_dnf_docker()    { PKG_MGR=dnf;    [ "$(prereq_package docker)" = "docker" ]; }
t_pacman_python() { PKG_MGR=pacman; [ "$(prereq_package python3)" = "python" ]; }
t_apt_python()    { PKG_MGR=apt;    [ "$(prereq_package python3)" = "python3" ]; }
t_rg_is_ripgrep() { [ "$(prereq_package rg)" = "ripgrep" ]; }
t_psql_is_pg()    { [ "$(prereq_package psql)" = "postgresql" ]; }
t_unknown_passes(){ [ "$(prereq_package curl)" = "curl" ]; }

printf 'package names\n'
it 'docker is docker.io on apt, which is the one that differs' t_apt_docker
it 'docker is docker on pacman' t_pacman_docker
it 'docker is docker on dnf' t_dnf_docker
it 'python3 is python on pacman' t_pacman_python
it 'python3 is python3 on apt' t_apt_python
it 'rg is the ripgrep package, not its command name' t_rg_is_ripgrep
it 'psql stands in for the postgresql server package' t_psql_is_pg
it 'an unmapped command falls through as itself' t_unknown_passes

# --- consent ----------------------------------------------------------------

installed=""
fake_install() { installed="$installed $*"; return 0; }

# Keep the real one before stubbing: the no-terminal path is tested below, and
# it is where a genuine bug lived -- `[ -r /dev/tty ]` passes in a process with
# no controlling terminal, the open then fails, and under `set -u` the unset
# reply aborted the whole installer instead of answering no.
eval "real_confirm() $(declare -f confirm | tail -n +2)"

# confirm() deliberately reads /dev/tty so a piped install still asks a human,
# which means stdin cannot drive it. What is under test here is what
# offer_to_install DOES with an answer, so the answer is stubbed.
answer="n"
confirm() { [ "$ASSUME_YES" = "1" ] && return 0; [ "$answer" = "y" ]; }

reset() {
    installed=""; answer="n"
    PKG_MGR=apt; PKG_INSTALL="fake_install"
    DRY_RUN=0; ASSUME_YES=0; INSTALL_PREREQS=0
}

# --yes must NOT be read as permission to install.
t_yes_is_not_consent() {
    reset; ASSUME_YES=1
    offer_to_install git && return 1   # must refuse
    [ -z "$installed" ]                # and must have installed nothing
}

# The explicit opt-in, and the only way an unattended run installs anything.
t_explicit_optin_installs() {
    reset; ASSUME_YES=1; INSTALL_PREREQS=1
    offer_to_install git rg || return 1
    [ "$installed" = " git ripgrep" ]
}

# Declining at the prompt installs nothing and says so.
t_declining_installs_nothing() {
    reset
    answer=n
    offer_to_install git && return 1
    [ -z "$installed" ]
}

# Accepting at the prompt installs exactly what was listed.
t_accepting_installs_the_list() {
    reset
    answer=y
    offer_to_install docker || return 1
    [ "$installed" = " docker.io" ]
}

# Anything other than yes is no: the default has to be the safe one.
t_empty_answer_is_no() {
    reset
    answer=""
    offer_to_install git && return 1
    [ -z "$installed" ]
}

# No package manager means no offer, rather than a confusing failure later.
t_no_package_manager() {
    reset; PKG_INSTALL=""
    answer=y
    offer_to_install git && return 1
    [ -z "$installed" ]
}

# Nothing missing is not an occasion to ask anything.
t_nothing_to_do() {
    reset
    offer_to_install && return 1
    [ -z "$installed" ]
}

# A dry run asks, then performs nothing -- `run` is what enforces that.
t_dry_run_installs_nothing() {
    reset; DRY_RUN=1
    answer=y
    offer_to_install git || return 1
    [ -z "$installed" ]
}

# This test body runs where there is no controlling terminal, which is exactly
# the condition being checked. It must answer no, quietly, and not abort.
t_no_terminal_answers_no() {
    reset
    local out
    out=$(real_confirm "Install them now?" 2>&1) && return 1
    case "$out" in *"no terminal to ask on"*) return 0 ;; *) return 1 ;; esac
}

printf 'consent\n'
it '--yes alone refuses to install and installs nothing' t_yes_is_not_consent
it 'INSTALL_PREREQS=1 is the explicit opt-in, and installs the mapped names' t_explicit_optin_installs
it 'answering no installs nothing' t_declining_installs_nothing
it 'answering yes installs exactly what was listed' t_accepting_installs_the_list
it 'anything that is not yes counts as no, so the default is the safe one' t_empty_answer_is_no
it 'no package manager means no install attempt' t_no_package_manager
it 'nothing missing asks nothing' t_nothing_to_do
it 'a dry run performs no install' t_dry_run_installs_nothing
it 'no controlling terminal answers no instead of aborting the installer' t_no_terminal_answers_no

printf '\n'
if [ "$failed" -gt 0 ]; then printf '%s of %s failed\n' "$failed" "$ran"; exit 1; fi
printf '%s passed\n' "$ran"
