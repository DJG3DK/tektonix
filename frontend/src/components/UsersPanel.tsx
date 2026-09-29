import { useEffect, useState } from "react";
import { createUser, deleteUser, listUsers, updateUserAccess } from "../api";
import type { CurrentUser } from "../types";
import "./UsersPanel.css";

interface Props {
  repos: string[];
  /** Whether this deployment is licensed to add accounts (agent/features.py).
   *  Managing the accounts that exist never depends on it. */
  canCreate: boolean;
}

function NewUserForm({ repos, onCreated }: { repos: string[]; onCreated: () => void }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [selectedRepos, setSelectedRepos] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [createdPassword, setCreatedPassword] = useState<string | null>(null);

  function toggleRepo(repo: string) {
    setSelectedRepos((prev) => {
      const next = new Set(prev);
      if (next.has(repo)) next.delete(repo);
      else next.add(repo);
      return next;
    });
  }

  async function handleCreate() {
    if (!email.trim() || !password || selectedRepos.size === 0) return;
    setSubmitting(true);
    setError(null);
    try {
      await createUser(email.trim(), password, "user", Array.from(selectedRepos));
      setCreatedPassword(password);
      setEmail("");
      setPassword("");
      setSelectedRepos(new Set());
      onCreated();
    } catch (err) {
      setError(err instanceof Error ? err.message : "failed to create user");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="users-new-form">
      <h2>Add a user</h2>
      <label className="form-field">
        <span>Email</span>
        <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="off" />
      </label>
      <label className="form-field">
        <span>Initial password</span>
        <input type="text" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="at least 12 characters" autoComplete="new-password" />
      </label>
      <div className="form-field">
        <span>Projects they can see and work on</span>
        <div className="repo-checkbox-list">
          {repos.map((r) => (
            <label key={r} className="repo-checkbox">
              <input type="checkbox" checked={selectedRepos.has(r)} onChange={() => toggleRepo(r)} />
              {r}
            </label>
          ))}
        </div>
      </div>
      {error && <div className="error-banner">{error}</div>}
      {createdPassword && (
        <div className="users-created-note">
          User created. They'll be asked to set their own password on first login.
        </div>
      )}
      <button
        className="btn btn-primary"
        disabled={submitting || !email.trim() || !password || selectedRepos.size === 0}
        onClick={handleCreate}
      >
        {submitting ? "Creating..." : "Create user"}
      </button>
    </div>
  );
}

function UserRow({ user, repos, onChanged }: { user: CurrentUser; repos: string[]; onChanged: () => void }) {
  const [selectedRepos, setSelectedRepos] = useState<Set<string>>(new Set(user.allowed_repos ?? []));
  const [saving, setSaving] = useState(false);
  const [deleting, setDeleting] = useState(false);
  // A failed save or delete used to be a button that went back to normal
  // with nothing said (2026-09-29 audit, U8).
  const [error, setError] = useState<string | null>(null);
  const changed = JSON.stringify([...selectedRepos].sort()) !== JSON.stringify([...(user.allowed_repos ?? [])].sort());

  function toggleRepo(repo: string) {
    setSelectedRepos((prev) => {
      const next = new Set(prev);
      if (next.has(repo)) next.delete(repo);
      else next.add(repo);
      return next;
    });
  }

  async function handleSave() {
    setSaving(true);
    setError(null);
    try {
      await updateUserAccess(user.id, Array.from(selectedRepos));
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : "saving access failed");
    } finally {
      setSaving(false);
    }
  }

  async function handleDelete() {
    if (!confirm(`Remove ${user.email}? They'll be logged out immediately.`)) return;
    setDeleting(true);
    setError(null);
    try {
      await deleteUser(user.id);
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : "removing the account failed");
    } finally {
      setDeleting(false);
    }
  }

  if (user.role === "admin") {
    return (
      <div className="users-row">
        <div className="users-row-email">
          {user.email} <span className="users-admin-badge">admin</span>
        </div>
        <div className="users-row-access">full access to every project</div>
      </div>
    );
  }

  return (
    <div className="users-row">
      <div className="users-row-email">{user.email}</div>
      <div className="repo-checkbox-list">
        {repos.map((r) => (
          <label key={r} className="repo-checkbox">
            <input type="checkbox" checked={selectedRepos.has(r)} onChange={() => toggleRepo(r)} />
            {r}
          </label>
        ))}
      </div>
      {error && <div className="error-banner" role="alert">{error}</div>}
      <div className="users-row-actions">
        {changed && (
          <button className="btn btn-primary btn-small" disabled={saving} onClick={handleSave}>
            {saving ? "Saving..." : "Save access"}
          </button>
        )}
        <button className="users-delete-btn" disabled={deleting} onClick={handleDelete}>
          {deleting ? "…" : "Remove"}
        </button>
      </div>
    </div>
  );
}

export function UsersPanel({ repos, canCreate }: Props) {
  const [users, setUsers] = useState<CurrentUser[]>([]);
  const [loading, setLoading] = useState(true);
  const [listError, setListError] = useState<string | null>(null);

  async function refresh() {
    try {
      setUsers(await listUsers());
      setListError(null);
    } catch (err) {
      setListError(err instanceof Error ? err.message : "could not load the accounts");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    // oxlint-disable-next-line react/set-state-in-effect -- the load it starts is async; state lands after the await, not in the effect
    refresh();
  }, []);

  return (
    <div className="users-panel">
      <h1 className="users-title">Users</h1>
      <p className="users-sub">Control who can log in and which projects they can see and work on.</p>

      {listError && <div className="error-banner" role="alert">{listError}</div>}
      {!loading && (
        <div className="users-list">
          {users.map((u) => (
            <UserRow key={u.id} user={u} repos={repos} onChanged={refresh} />
          ))}
        </div>
      )}

      {canCreate ? (
        <NewUserForm repos={repos} onCreated={refresh} />
      ) : (
        /* The route that mints accounts answers 403 here; a form that can
           only ever fail is worse than a sentence. The accounts above are
           unaffected: the switch is on making new ones, never on signing in. */
        <p className="users-sub" data-testid="users-create-unavailable">
          Adding accounts is a licensed feature this deployment does not have. The accounts
          above keep working and can be managed here.
        </p>
      )}
    </div>
  );
}
