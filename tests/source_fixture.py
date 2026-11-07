"""Freeze current copied test bytes without rewriting historical evaluation records."""
import json
from opsfleet_assistant.evaluation_contracts import canonical, digest


def freeze_copied_source(root):
    path = root/'docs/remediation/EVALUATION_SOURCE_MANIFEST.json'
    source = json.loads(path.read_text())
    source['files'] = {name: digest(root/name) for name in source['files']}
    source['revision'] = canonical(source['files'])
    path.write_text(json.dumps(source))
