# LibreNMS Deployment Journey

This document captures the steps and fixes needed to run LibreNMS at the
`/librenms` subpath in this lab, and why each change was required.

## Goal

Expose LibreNMS under `/librenms` on the MetalLB VIP while keeping it stable
and reproducible from Ansible.

## Key challenges and fixes

### 1) Root-path assumptions in LibreNMS

LibreNMS generates absolute URLs (`http://<host>/...`) and expects to run at
`/`. This breaks subpath deployments.

Fixes applied:
- Set `LIBRENMS_BASE_URL` and `config['base_url']` to the external URL.
- Add an nginx proxy that rewrites inbound paths to `/` and rewrites outbound
  content (HTML/CSS/JS) to include `/librenms`.
- Disable upstream compression so `sub_filter` can rewrite HTML.

Relevant files:
- `k3s-lab-bootstrap/roles/librenms/templates/values.yml.j2`
- `k3s-lab-bootstrap/roles/librenms/templates/nginx.conf.j2`
- `k3s-lab-bootstrap/roles/librenms/tasks/main.yml`

### 2) Redirects and absolute URLs

Initial redirects pointed to `http://<vip>/login` and skipped `/librenms`.
The nginx proxy now rewrites:

- `Location` headers to include `/librenms`
- absolute `http(s)://<host>/` links to `http(s)://<host>/librenms/`

### 3) ConfigMap changes not applied

The nginx config is a ConfigMap; pods do not reload it automatically. A
checksum annotation was added to the proxy Deployment to force a rollout when
the config changes.

### 4) Admin user creation

LibreNMS blocks the UI until an admin is created. The UI postback can fail
silently during early boot. The playbook now:

- Checks the user count in MySQL.
- Creates a user with `lnms user:add` when the table is empty.
- Generates a random password if one is not provided and prints it.

### 5) Scheduler validation warning

Installer validation may report "Scheduler is not running". Inside the pod,
copy the cron file:

```bash
sudo k3s kubectl exec -n librenms deploy/librenms-frontend -- /bin/sh -c \
  "cp /opt/librenms/dist/librenms-scheduler.cron /etc/cron.d/librenms-scheduler && chmod 0644 /etc/cron.d/librenms-scheduler"
```

This is currently a manual step; consider adding it to the playbook if you
want it automated.

## Useful commands

Check install step status:
```bash
curl -s http://<vip>/librenms/install/ajax/steps
```

Create a user manually:
```bash
sudo k3s kubectl exec -n librenms deploy/librenms-frontend -- /bin/sh -c \
  "su -s /bin/sh librenms -c '/opt/librenms/lnms user:add admin -p <password> -r admin -e admin@example.com -n'"
```

Inspect users in MySQL:
```bash
sudo k3s kubectl exec -n librenms librenms-mysql-0 -- mysql -u librenms -p'<password>' -D librenms \
  -e "SELECT username,email FROM users;"
```
