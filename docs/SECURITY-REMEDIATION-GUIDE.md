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

OpenBao is initialized with Shamir key shares. The OpenBao role sets the count
and the threshold with `openbao_shamir_shares` and `openbao_shamir_threshold`:

- The three-node lab layout uses the default, 5 shares with a threshold of 3.
- The v0.1 single-node sandbox profile uses 1 share with a threshold of 1. Its
  one share is in the automation file that OpenBao init writes on your machine.

In the steps below, *threshold* means the threshold your environment uses.

### Before you start

You need:

- Access to the control node of the cluster, with a working kubeconfig.
- The `bao` command-line tool, only if you unseal from your own machine.
- As many key shares as your threshold, from your own custody locations.

### Procedure

1. Retrieve as many key shares as your threshold from your custody locations. Do not paste them
   into a shared terminal log or a chat.

2. Open a shell on the control node:

   ```bash
   ssh <user>@<control-node>
   ```

3. Unseal from inside the OpenBao pod. The pod already sets `BAO_ADDR` to
   `https://127.0.0.1:8200` and `BAO_CACERT` to the certificate it serves, so
   the `bao` tool verifies TLS with no extra flags. Run the command as many
   times as your threshold, and enter one share at each prompt:

   ```bash
   sudo kubectl --kubeconfig /etc/rancher/k3s/k3s.yaml \
     -n openbao exec -it openbao-0 -- bao operator unseal
   ```

   The share goes in at the prompt, so it never appears in the command line
   or the shell history.

4. Confirm the result:

   ```bash
   sudo kubectl --kubeconfig /etc/rancher/k3s/k3s.yaml \
     -n openbao exec openbao-0 -- bao status
   ```

   The output shows `Sealed: false` when the unseal succeeded.

To run the `bao` tool from your own machine instead, port-forward the service
and trust the self-signed certificate from the `openbao-tls` secret, which
cert-manager issues for `127.0.0.1` and the service DNS names:

```bash
kubectl -n openbao get secret openbao-tls \
  -o jsonpath='{.data.tls\.crt}' | base64 -d > <ca-file>
kubectl -n openbao port-forward svc/openbao 8200:8200 &
export BAO_ADDR=https://127.0.0.1:8200
export BAO_CACERT=<ca-file>
bao operator unseal
bao status
```

Never turn off certificate verification for these commands.

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
