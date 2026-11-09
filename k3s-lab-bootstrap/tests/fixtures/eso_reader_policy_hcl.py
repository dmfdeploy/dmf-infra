"""Print the HCL text of the eso-reader policy that the OpenBao role writes. The fake
bao returns the text of the file named in FAKE_ESO_POLICY_FILE when the verifier reads
sys/policies/acl/eso-reader, so the verifier's policy check runs against the policy as
the role writes it.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from test_openbao_eso_reader_policy import eso_reader_hcl  # noqa: E402

print(eso_reader_hcl())
