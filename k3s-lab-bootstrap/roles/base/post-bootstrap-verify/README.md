# post-bootstrap-verify

Disposable post-bootstrap verification for network and ingress correctness.

It proves, in order:

- cross-node pod-to-pod traffic
- cross-node Service traffic
- cluster DNS reachability from a pod
- ingress path through Traefik
- NodePort or LoadBalancer readiness on the Traefik service
