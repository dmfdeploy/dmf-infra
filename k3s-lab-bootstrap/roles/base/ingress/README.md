# ingress

Environment-selected ingress bootstrap for the DMF platform.

This role keeps the north-south contract stable while selecting the underlying
provider path from inventory values:

- `cloud-native` — delegates CCM/LB install to an env-supplied task file
- `metallb-l2`   — includes the generic `metallb` role in L2 mode
- `metallb-bgp`  — includes the generic `metallb` role in BGP mode
- `nodeport-only` — no provider install; Traefik patched to `NodePort`

In `cloud-native` mode the env inventory supplies the provider implementation
via `cluster_ingress_provider_tasks`, e.g.

```yaml
cluster_ingress_provider_tasks: "{{ inventory_dir }}/../../tasks/hetzner/ccm.yml"
```

This keeps the generic repo provider-agnostic: no cloud manifest URL, no cloud
API token handling, and no vendor-named roles live here. Cloud specifics live
with the environment that owns the cloud account.

After the provider step the role patches the bundled Traefik service to the
right exposure type and annotations for the selected mode.
