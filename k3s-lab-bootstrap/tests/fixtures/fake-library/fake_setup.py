#!/usr/bin/python
# WANT_JSON
"""Stand-in for ansible.builtin.setup in tests.

It takes the same `gather_subset` argument. When the subset names `network` (or
`all`), it returns the facts given in `facts`, the way the real module returns
the network facts of a node. Otherwise it returns no facts, as the real module
does for a subset that leaves the network out. Any other option fails the task,
because an option such as `filter` changes which facts the real module returns
and this stand-in does not model that.
"""
import json
import sys

KNOWN = {"gather_subset", "facts"}


def main():
    with open(sys.argv[1], encoding="utf-8") as handle:
        args = json.load(handle)
    unknown = sorted(k for k in set(args) - KNOWN if not k.startswith("_ansible_"))
    if unknown:
        print(json.dumps({
            "failed": True,
            "msg": "the stand-in setup does not model these options: "
                   + ", ".join(unknown),
        }))
        return
    subset = args.get("gather_subset") or ["all"]
    if isinstance(subset, str):
        subset = [subset]
    gathered = "network" in subset or "all" in subset
    facts = args.get("facts", {}) if gathered else {}
    print(json.dumps({"changed": False, "ansible_facts": facts}))


if __name__ == "__main__":
    main()
