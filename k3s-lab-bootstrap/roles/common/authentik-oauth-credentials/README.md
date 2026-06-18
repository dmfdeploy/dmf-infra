# common/authentik-oauth-credentials

Shared resolver for Authentik `OAuth2Provider` `client_id` / `client_secret`.
Encapsulates the wait → retried exec → sentinel-delimited parse sequence so
consumer roles don't each hand-roll their own.

## Why this role exists

Six consumer roles (forgejo, netbox, grafana, librenms, zot, cms) read an
Authentik OAuth2Provider's credentials by `kubectl exec`-ing `ak shell` into
`deploy/<release>-worker`. The raw exec is unguarded against the worker
restarting (liveness flap) and the output parse uses `stdout_lines | last |
from_json`, which breaks on any trailing `ak` log line.

This role adds:

1. A **readiness gate** — waits for the Authentik worker Deployment to report
   `readyReplicas >= authentik_worker_replicas` before exec.
2. **Retried exec** — retries the `ak shell` call on non-zero rc (covers
   transient worker-pod unavailability).
3. **Sentinel-delimited parse** — the Python snippet wraps the JSON in
   `__AK_JSON__…__AK_JSON__` sentinels; the parse uses `regex_search` to
   extract exactly the payload, immune to trailing log noise.
4. **Block/rescue diagnostic** — on exhausted retries, surfaces `rc` and
   the last `stderr` line (never the secret, which only appears on stdout).

## Inputs

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `authentik_oauth_provider_name` | **yes** | — | Authentik OAuth2Provider name to look up |
| `authentik_oauth_result_var` | no | `authentik_oauth_credentials` | Name of the dict fact emitted |
| `authentik_namespace` | no | `authentik` | Namespace of the Authentik install |
| `authentik_release_name` | no | `authentik` | Helm release name (worker pod is `deploy/<release>-worker`) |
| `authentik_worker_replicas` | no | `1` | Expected worker replica count for readiness gate |
| `authentik_oauth_ready_retries` | no | `60` | Retries for the worker readiness gate |
| `authentik_oauth_ready_delay` | no | `10` | Delay (seconds) between readiness retries |
| `authentik_oauth_exec_retries` | no | `30` | Retries for the ak shell exec |
| `authentik_oauth_exec_delay` | no | `5` | Delay (seconds) between exec retries |

## Output

A single dict fact named by `authentik_oauth_result_var` (default
`authentik_oauth_credentials`) with keys `.client_id` and `.client_secret`.

## Usage

```yaml
- name: Read Forgejo OAuth credentials from Authentik
  ansible.builtin.include_role:
    name: common/authentik-oauth-credentials
  vars:
    authentik_oauth_provider_name: "{{ forgejo_oauth_provider_name }}"
    authentik_oauth_result_var: forgejo_oauth_provider_credentials_parsed
  when: forgejo_oauth_enabled | bool
```
