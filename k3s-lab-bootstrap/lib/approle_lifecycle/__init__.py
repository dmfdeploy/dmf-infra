"""Registry model, delivery and readiness adapters, and the setup-only SecretID
path for the finite-TTL OpenBao AppRole lifecycle (dmfdeploy/dmfdeploy#11).

The package uses the standard library only. The adapters take client objects,
so the rotation transaction can run them from a playbook, a job or a test.
"""
