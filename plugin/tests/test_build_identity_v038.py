import importlib.util
import json

import pytest


def build(root):
    assert importlib.util.find_spec('patent_drawing.build_identity'),'runtime build identity missing'
    from patent_drawing.build_identity import runtime_build_sha256
    return runtime_build_sha256(root)


def test_repair_binding_changes_when_runtime_bytes_change(tmp_path):
    (tmp_path/'_assets').mkdir()
    (tmp_path/'_assets/runtime-members.json').write_text(json.dumps(['main.py','_assets/runtime-members.json']))
    (tmp_path/'main.py').write_text('original code')
    before=build(tmp_path)
    (tmp_path/'main.py').write_text('different code')
    assert build(tmp_path)!=before


def test_missing_manifest_cannot_claim_build(tmp_path):
    with pytest.raises(ValueError,match='build'):
        build(tmp_path)


def test_manifest_cannot_read_outside_runtime_root(tmp_path):
    (tmp_path/'_assets').mkdir()
    (tmp_path/'_assets/runtime-members.json').write_text(json.dumps(['../outside']))
    with pytest.raises(ValueError,match='build'):
        build(tmp_path)
