# Security remediation guide

This guide describes the OpenBao recovery procedure that the dmf-infra
playbooks assume, and the practices that keep recovery material safe. It uses
placeholders for every environment-specific value. For the design behind the
procedure, see [OpenBao bootstrap security model](openbao-bootstrap-security-model.md).

To report a vulnerability, see [SECURITY.md](../SECURITY.md).

## Principles

- Keep recovery material out of git, chat transcripts, and ad hoc temporary
  files.
- Keep at least a threshold quorum of key shares readable, protected, and
  stored in separate places. Your environment decides where.
- Dispose of the initial root token after bootstrap. The OpenBao role does this
  by default (`openbao_dispose_root_token: true`). Use the key shares for
  privileged recovery instead.
- Keep the break-glass automation file outside the repository, with mode `0600`.

## Emergency unseal

OpenBao is initialized with Shamir key shares. The default threshold is 3 of 5.

### Before you start

You need:

- Access to the control node of the cluster, with a working kubeconfig.
- The `bao` command-line tool.
- At least three key shares from your own custody locations.

### Procedure

1. Retrieve three key shares from your custody locations. Do not paste them
   into a shared terminal log or a chat.

2. Open a shell on the control node:

   ```bash
   ssh <user>@<control-node>
   ```

3. Forward the OpenBao service port:

   ```bash
   kubectl port-forward -n openbao svc/openbao 8200:8200 &
   ```

4. Unseal with the `bao` CLI. Run the command three times and enter one share
   each time:

   ```bash
   bao operator unseal
   ```

5. Confirm the result:

   ```bash
   bao status
   ```

   The output shows `Sealed: false` when the unseal succeeded.

### What to avoid

- Do not reuse shares by sending them over email or chat.
- Do not store shares in version control.
- Do not try to recreate the root token by hand.

### If you lose the quorum

Without the threshold number of shares, OpenBao data cannot be recovered. Plan
your custody before you run `bao operator init`.

## Related documents

- [OpenBao bootstrap security model](openbao-bootstrap-security-model.md)
- [Security and compliance framework plan](security-compliance-framework-plan.md)
