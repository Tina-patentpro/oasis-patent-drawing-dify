import pytest

from patent_drawing import source_contract
from patent_drawing.operation_store import digest


@pytest.mark.parametrize('mutation,expected', [
    (None, 'PASS'),
    ('parent_region', 'FAIL'),
    ('parent_artifact_sha256', 'FAIL'),
    ('child_locator', 'FAIL'),
    ('parent_targets', 'FAIL'),
    ('source_evidence_ids', 'UNKNOWN'),
])
def test_detail_binding_checks_actual_locator_target_and_revision(mutation, expected):
    parent, child = digest('parent'), digest('child')
    figures = {
        'main': {'sha256': parent, 'size': [600, 600],
                 'locators': {'A': [100, 100]}, 'targets': {'hole': [200, 200, 260, 250]}},
        'local': {'sha256': child, 'size': [600, 600], 'locators': {'A': [100, 100]},
                  'targets': {'hole': [220, 220, 280, 270]}},
    }
    row = {'parent_view_id': 'main', 'child_view_id': 'local', 'locator': 'A',
           'parent_region': [50, 50, 280, 280], 'child_region': [50, 50, 300, 300],
           'feature_ids': ['hole'], 'source_evidence_ids': ['drawing'],
           'parent_artifact_sha256': parent, 'child_artifact_sha256': child,
           'parent_targets': {'hole': [220, 230]}, 'child_targets': {'hole': [240, 240]},
           'representation': 'projection'}
    if mutation == 'parent_region':
        row[mutation] = [0, 0, 50, 50]
    elif mutation == 'parent_artifact_sha256':
        row[mutation] = digest('stale')
    elif mutation == 'child_locator':
        figures['local']['locators'] = {'B': [100, 100]}
    elif mutation == 'parent_targets':
        row[mutation] = {'hole': [120, 120]}
    elif mutation == 'source_evidence_ids':
        row[mutation] = []
    validator = getattr(source_contract, 'validate_detail_bindings', lambda *_: {'status': 'missing'})
    assert validator([row], figures, {'drawing'})['status'] == expected
