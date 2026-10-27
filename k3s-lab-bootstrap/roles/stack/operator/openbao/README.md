# OpenBao bootstrap identity boundary

## Fresh initialization and existing environments

The born-inventory setup issuer and the dmf-promsd AppRole metadata observer
Kubernetes-auth roles are created only during fresh OpenBao initialization.
On an already-initialized instance, the role's rerun path can reconcile ACL
policies with the policy-reconciler identity, but it does not create or update
these Kubernetes-auth roles.

Do not treat either identity as provisioned on an existing environment or
deploy a caller that depends on it. Applying those bindings to an initialized
instance requires a separately reviewed privileged migration. The regular
policy-reconciler must not gain auth-role management rights to make this
convenient; no automatic recovery path is implemented here.

This boundary keeps the bootstrap foundation reviewable while the existing
OpenBao authority model remains unchanged. It does not claim an existing
environment is ready for setup-issuer or observer use.
