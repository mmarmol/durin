"""Environment access is a finding only when it correlates with a real risk.

The rule used to be a bare `os.environ` regex at `caution`, so every script that
read any environment variable was labelled "exfil-adjacent" — including one whose
only env read is a bucket name with a default. Reading configuration from the
environment is ordinary; reading a *credential* and having a way to send it is
the shape worth flagging.
"""

from durin.security.skill_scan import scan_skill

_SECRET_AND_OUTBOUND = """
import os, requests
API_KEY = os.environ.get("ACME_API_KEY")
requests.get("https://api.acme.com/v1/things", headers={"X-API-Key": API_KEY})
"""

_SECRET_AND_SDK = """
import os, boto3
KEY = os.environ["AWS_SECRET_ACCESS_KEY"]
boto3.client("s3").list_buckets()
"""

_CONFIG_AND_OUTBOUND = """
import os, boto3
BUCKET = os.environ.get("TRACING_EVENTS_BUCKET", "events-prd")
boto3.client("s3").get_object(Bucket=BUCKET, Key="k")
"""

_SECRET_NO_OUTBOUND = """
import os
TOKEN = os.environ.get("ACME_TOKEN")
print(len(TOKEN))
"""

# The exact text the rule has always emitted. Findings are fingerprinted as
# category|where|detail and acked in the review store, so changing this string
# silently invalidates every existing user review of a flagged skill.
_DETAIL = "environment access (exfil-adjacent)"


def _skill(tmp_path, name, script):
    d = tmp_path / name
    (d / "scripts").mkdir(parents=True)
    (d / "SKILL.md").write_text(f"---\nname: {name}\ndescription: d\n---\nbody\n")
    (d / "scripts" / "s.py").write_text(script)
    return d


def _env_findings(report):
    return [f for f in report.findings if f.detail == _DETAIL]


def test_secret_read_plus_outbound_is_still_flagged(tmp_path):
    rep = scan_skill(_skill(tmp_path, "a", _SECRET_AND_OUTBOUND))
    assert len(_env_findings(rep)) == 1


def test_secret_read_plus_sdk_call_is_flagged(tmp_path):
    """An SDK is an outbound call too — grepping for an http:// literal misses it."""
    rep = scan_skill(_skill(tmp_path, "b", _SECRET_AND_SDK))
    assert len(_env_findings(rep)) == 1


def test_reading_plain_config_is_not_a_finding(tmp_path):
    """The real false positive: a bucket name with a default, and an SDK call."""
    rep = scan_skill(_skill(tmp_path, "c", _CONFIG_AND_OUTBOUND))
    assert _env_findings(rep) == []


def test_secret_read_with_no_way_out_is_not_a_finding(tmp_path):
    rep = scan_skill(_skill(tmp_path, "d", _SECRET_NO_OUTBOUND))
    assert _env_findings(rep) == []


def test_the_finding_text_is_unchanged(tmp_path):
    """Pinned so existing acked reviews stay valid — a new string would read as a
    new finding and silently invalidate them."""
    rep = scan_skill(_skill(tmp_path, "e", _SECRET_AND_OUTBOUND))
    f = _env_findings(rep)[0]
    assert (f.category, f.severity, f.where) == ("dangerous_code", "caution", "scripts/s.py")
