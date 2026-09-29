//! What a pass over the stack does, decided from what it sees. Nothing in
//! here runs docker or asks the network, so every decision is a table test;
//! stack.rs gathers the facts and carries the decision out.

use crate::stack::{image_ref, local_name, newer_than, record_is_proven, Version, IMAGES};

/// The agent, as the health route answered.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Agent {
    /// Nothing answered: the stack is down, or not yet up.
    Down,
    /// Answered, with a task in flight, or too old to say (busy_in).
    Busy,
    /// Answered, nothing in flight.
    Idle,
}

/// What the stack knows before it decides.
pub struct Seen<'a> {
    /// The release this app was built from (stack::release_tag).
    pub app_tag: &'a str,
    /// version.json, if there is one.
    pub record: Option<&'a Version>,
    /// The agent image Docker has under the local name, if any.
    pub local_id: Option<&'a str>,
    /// The fingerprint of the compose file this app ships.
    pub compose: &'a str,
    pub agent: Agent,
}

/// What to do about the stack this app runs.
#[derive(Debug, PartialEq, Eq)]
pub enum StackMove {
    /// This app's images, started under this app's compose file.
    Current,
    /// Something to do, and a task in flight: not now.
    Busy,
    /// Pull this app's images. Then restart the stack on them when it was
    /// running; a stopped stack starts on them with the next Start.
    Pull { restart: bool },
    /// The images are this app's, but the compose file they were started
    /// under is not this app's. 2026-09-29: an rc16 app ran rc17 images
    /// under rc16's compose file, and the `SANDBOX_*` settings rc17 added
    /// never reached the agent.
    Recompose { restart: bool },
}

pub fn own_release_move(seen: &Seen) -> StackMove {
    let proven = seen
        .record
        .is_some_and(|r| record_is_proven(r, seen.local_id));
    let images_current = proven
        && seen
            .record
            .is_some_and(|r| !newer_than(seen.app_tag, &r.tag));
    let compose_current = seen
        .record
        .is_some_and(|r| r.compose.as_deref() == Some(seen.compose));
    if images_current && compose_current {
        return StackMove::Current;
    }
    if seen.agent == Agent::Busy {
        return StackMove::Busy;
    }
    let restart = seen.agent == Agent::Idle;
    if images_current {
        StackMove::Recompose { restart }
    } else {
        StackMove::Pull { restart }
    }
}

/// Why an automatic pass does nothing, decided before it touches anything.
/// 2026-09-29: the first pass, two minutes after launch, found no record
/// under a first install still pulling, and started a second pull and `up`
/// beside it.
pub fn pass_skip_reason(
    settings_ready: bool,
    installed: bool,
    other_operation: bool,
) -> Option<&'static str> {
    if other_operation {
        return Some("a stack operation is in progress");
    }
    if !settings_ready {
        return Some("the settings are not complete yet");
    }
    if !installed {
        return Some("the stack is not installed yet");
    }
    None
}

/// Whether the health route's answer says a task is in flight. An answer
/// that does not say (an agent older than rc7, which added `busy`; or not
/// JSON at all) counts as busy: a restart on a guess would pull a running
/// task's containers out from under it.
pub fn busy_in(answer: &str) -> bool {
    match serde_json::from_str::<serde_json::Value>(answer)
        .ok()
        .and_then(|v| v.get("busy")?.as_u64())
    {
        Some(count) => count > 0,
        None => true,
    }
}

/// The docker commands that bring one release's images in, in order: every
/// pull, then every tag. A failure on the third pull used to leave two
/// images retagged to the new release and two on the old.
pub fn pull_commands(tag: &str) -> Vec<Vec<String>> {
    let pulls = IMAGES
        .iter()
        .map(|name| vec!["pull".to_string(), image_ref(name, tag)]);
    let tags = IMAGES
        .iter()
        .map(|name| vec!["tag".to_string(), image_ref(name, tag), local_name(name)]);
    pulls.chain(tags).collect()
}

/// Start needs this app's release, but not the registry: when the pull
/// fails and images are already here, Start goes ahead on them and says so.
/// With no images at all there is nothing to start.
pub fn start_on_local_images(pull_error: &str, images_present: bool) -> Result<String, String> {
    if images_present {
        Ok(format!(
            "{pull_error}. Starting the images already on this machine; the update is tried again later."
        ))
    } else {
        Err(pull_error.to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn record(tag: &str, image_id: Option<&str>, compose: Option<&str>) -> Version {
        Version {
            tag: tag.into(),
            image_id: image_id.map(str::to_string),
            compose: compose.map(str::to_string),
        }
    }

    fn seen<'a>(record: Option<&'a Version>, agent: Agent) -> Seen<'a> {
        Seen {
            app_tag: "v0.9.0-rc17",
            record,
            local_id: Some("sha256:abc"),
            compose: "c17",
            agent,
        }
    }

    #[test]
    fn a_stack_on_this_apps_release_and_compose_file_is_left_alone() {
        let r = record("v0.9.0-rc17", Some("sha256:abc"), Some("c17"));
        for agent in [Agent::Down, Agent::Busy, Agent::Idle] {
            assert_eq!(own_release_move(&seen(Some(&r), agent)), StackMove::Current);
        }
        let ahead = record("v0.9.0-rc18", Some("sha256:abc"), Some("c17"));
        assert_eq!(
            own_release_move(&seen(Some(&ahead), Agent::Idle)),
            StackMove::Current,
            "a stack moved ahead by hand stays there"
        );
    }

    #[test]
    fn an_older_or_unproven_stack_gets_this_apps_images() {
        let older = record("v0.9.0-rc16", Some("sha256:abc"), Some("c17"));
        assert_eq!(
            own_release_move(&seen(Some(&older), Agent::Idle)),
            StackMove::Pull { restart: true }
        );
        assert_eq!(
            own_release_move(&seen(Some(&older), Agent::Down)),
            StackMove::Pull { restart: false },
            "a stack the operator stopped is not started for an update"
        );
        assert_eq!(
            own_release_move(&seen(Some(&older), Agent::Busy)),
            StackMove::Busy
        );
        let unproven = record("v0.9.0-rc17", None, Some("c17"));
        assert_eq!(
            own_release_move(&seen(Some(&unproven), Agent::Idle)),
            StackMove::Pull { restart: true },
            "a record without its image id is not trusted"
        );
        assert_eq!(
            own_release_move(&seen(None, Agent::Down)),
            StackMove::Pull { restart: false },
            "no record: a first install by the pass, not started"
        );
    }

    #[test]
    fn this_apps_images_under_another_compose_file_are_restarted_under_this_one() {
        let stale = record("v0.9.0-rc17", Some("sha256:abc"), Some("c16"));
        assert_eq!(
            own_release_move(&seen(Some(&stale), Agent::Idle)),
            StackMove::Recompose { restart: true }
        );
        assert_eq!(
            own_release_move(&seen(Some(&stale), Agent::Down)),
            StackMove::Recompose { restart: false }
        );
        assert_eq!(
            own_release_move(&seen(Some(&stale), Agent::Busy)),
            StackMove::Busy
        );
        let unstamped = record("v0.9.0-rc17", Some("sha256:abc"), None);
        assert_eq!(
            own_release_move(&seen(Some(&unstamped), Agent::Idle)),
            StackMove::Recompose { restart: true },
            "a record from before the fingerprint was kept"
        );
        let old: Version = serde_json::from_str(r#"{"tag":"v0.9.0-rc13","image_id":"x"}"#).unwrap();
        assert_eq!(old.compose, None, "the old record still parses");
    }

    #[test]
    fn the_pass_waits_for_settings_a_record_and_the_lock() {
        assert_eq!(
            pass_skip_reason(true, true, true),
            Some("a stack operation is in progress")
        );
        assert_eq!(
            pass_skip_reason(false, false, false),
            Some("the settings are not complete yet")
        );
        assert_eq!(
            pass_skip_reason(true, false, false),
            Some("the stack is not installed yet"),
            "a first install still pulling has no record yet"
        );
        assert_eq!(pass_skip_reason(true, true, false), None);
    }

    #[test]
    fn an_agent_that_does_not_say_counts_as_busy() {
        assert!(!busy_in(r#"{"status":"ok","busy":0}"#));
        assert!(busy_in(r#"{"status":"ok","busy":2}"#));
        assert!(
            busy_in(r#"{"status":"ok"}"#),
            "before rc7 the route had no busy field, and the stack was restarted mid-task"
        );
        assert!(busy_in("<html>502</html>"));
        assert!(busy_in(""));
    }

    #[test]
    fn every_image_is_pulled_before_any_is_retagged() {
        let cmds = pull_commands("v0.9.0-rc17");
        assert_eq!(cmds.len(), 2 * IMAGES.len());
        let (pulls, tags) = cmds.split_at(IMAGES.len());
        assert!(pulls.iter().all(|c| c[0] == "pull"));
        assert!(tags.iter().all(|c| c[0] == "tag"));
        assert_eq!(
            pulls[0],
            vec!["pull", "ghcr.io/djg3dk/tektonix-agent:v0.9.0-rc17"]
        );
        assert_eq!(
            tags[0],
            vec![
                "tag",
                "ghcr.io/djg3dk/tektonix-agent:v0.9.0-rc17",
                "tektonix-agent:latest"
            ]
        );
    }

    #[test]
    fn start_goes_ahead_on_local_images_when_the_registry_is_away() {
        let warned = start_on_local_images("could not pull x (exit 1)", true).unwrap();
        assert!(warned.starts_with("could not pull x (exit 1). Starting the images already"));
        assert_eq!(
            start_on_local_images("could not pull x (exit 1)", false),
            Err("could not pull x (exit 1)".to_string()),
            "nothing to start"
        );
    }
}
