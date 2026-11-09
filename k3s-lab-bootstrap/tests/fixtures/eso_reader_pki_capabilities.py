"""Print, as a JSON list, the capabilities that the eso-reader policy in the OpenBao
role grants on the PKI mount. An empty list prints "[]" and means no grant. The fake
bao holds a token to these capabilities when a test sets FAKE_ESO_PKI_GRANT to the
output, so the verifier's PKI probe runs against the policy as the role writes it.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_openbao_eso_reader_policy import pki_capabilities  # noqa: E402

print(json.dumps(pki_capabilities()))
