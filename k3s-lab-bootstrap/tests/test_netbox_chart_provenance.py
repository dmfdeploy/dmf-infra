"""The vendored NetBox chart matches its provenance record.

charts/netbox/PROVENANCE.yml records the upstream repository, version and SHA-256
digest of the tarball under charts/netbox/charts/. These tests fail when the tracked
file differs from the digest, or when the record, Chart.yaml and Chart.lock name
different versions or repositories. Set the environment variable NETBOX_CHART_DIR
to check another copy of the chart directory.
"""
import hashlib
import os
import unittest
from pathlib import Path

import yaml

DEFAULT_DIR = Path(__file__).resolve().parents[1] / "charts/netbox"
CHART_DIR = Path(os.environ.get("NETBOX_CHART_DIR", DEFAULT_DIR))


def load(name):
    return yaml.safe_load((CHART_DIR / name).read_text(encoding="utf-8"))


class NetboxChartProvenance(unittest.TestCase):
    def test_tarball_matches_recorded_digest(self):
        record = load("PROVENANCE.yml")
        data = (CHART_DIR / record["chart_file"]).read_bytes()
        self.assertEqual(hashlib.sha256(data).hexdigest(), record["sha256"])

    def test_record_names_the_pinned_dependency(self):
        record = load("PROVENANCE.yml")
        for source in ("Chart.yaml", "Chart.lock"):
            dep = load(source)["dependencies"][0]
            self.assertEqual(dep["name"], "netbox", source)
            self.assertEqual(str(dep["version"]), record["version"], source)
            self.assertEqual(dep["repository"], record["repository"], source)
        self.assertEqual(Path(record["chart_file"]).name, f"netbox-{record['version']}.tgz")


if __name__ == "__main__":
    unittest.main()
