import importlib
import pytest


def policy():
    assert importlib.util.find_spec('patent_drawing.cn_policy'), 'CN drawing/type policy missing'
    return importlib.import_module('patent_drawing.cn_policy')


def test_utility_model_flow_only_does_not_meet_product_drawing_requirement():
    result = policy().check_policy({'patent_type': 'utility_model', 'technical_types': ['computer_method'],
        'views': [{'type': 'flowchart'}]})
    assert any(x['rule'] == 'CN-UTILITY-STRUCTURE' and x['verdict'] == 'FAIL' for x in result)


@pytest.mark.parametrize('technology, expected', [
    ('mechanical', 'assembly'), ('electrical', 'circuit'),
    ('computer_method', 'flowchart'), ('communication', 'sequence'),
    ('manufacturing_process', 'process_flow'), ('chemical_material', 'process_flow'),
    ('bio_medical', 'experimental_evidence'), ('semiconductor', 'layer_section'),
])
def test_type_specific_recommendations(technology, expected):
    recommendations = policy().recommend_views([technology])
    assert expected in {x['type'] for x in recommendations}
    assert all(x['legal_minimum_count'] is False for x in recommendations)


def test_no_fake_measurement_when_chemical_case_lacks_data():
    result = policy().check_policy({'patent_type': 'invention', 'technical_types': ['chemical_material'],
        'views': [{'type': 'experimental_evidence', 'data_provenance': None}]})
    assert any(x['rule'] == 'BP-NO-FABRICATED-DATA' and x['verdict'] == 'FAIL' for x in result)


def test_necessary_flowchart_words_are_allowed_and_optional_labels_not_forced():
    result = policy().check_policy({'patent_type': 'invention', 'technical_types': ['computer_method'],
        'views': [{'type': 'flowchart', 'necessary_words': True, 'has_words': True}]})
    assert not any(x['verdict'] == 'FAIL' for x in result)


def test_no_automatic_filing_certificate_without_full_spec_and_visual_review():
    result = policy().check_policy({'patent_type': 'invention', 'technical_types': ['mechanical'],
        'views': [{'type': 'assembly'}]})
    assert any(x['verdict'] == 'UNKNOWN' for x in result)


def test_color_and_microscopy_exception_distinguish_patent_type():
    example = {'technical_types': ['bio_medical'], 'views': [{'type': 'experimental_evidence',
        'photo': True, 'photo_purpose': 'microscopy', 'data_provenance': 'source-1', 'color': True, 'color_reason': '必要组织染色区分'}]}
    invention = policy().check_policy({**example, 'patent_type': 'invention'})
    utility = policy().check_policy({**example, 'patent_type': 'utility_model'})
    assert not any(x['rule'] == 'CN-PHOTO' and x['verdict'] == 'FAIL' for x in invention)
    assert any(x['rule'] == 'CN-PHOTO' and x['verdict'] == 'FAIL' for x in utility)


def test_design_is_not_processed_as_invention_structure_diagram():
    result = policy().check_policy({'patent_type': 'design', 'technical_types': ['mechanical'],
        'views': [{'type': 'assembly'}]})
    assert any(x['rule'] == 'CN-DESIGN-ROUTE' and x['verdict'] == 'FAIL' for x in result)


def test_reference_marks_require_arabic_numbers_and_specification_mapping():
    result = policy().check_policy({'patent_type': 'invention', 'technical_types': ['mechanical'],
        'views': [{'type': 'assembly', 'labels': ['A']}], 'specification_labels': ['1']})
    assert any(x['rule'] == 'CN-MARK-NUMERIC' and x['verdict'] == 'FAIL' for x in result)
    assert any(x['rule'] == 'CN-MARK-SPEC' and x['verdict'] == 'FAIL' for x in result)


def test_post_filing_addition_requires_original_filed_support():
    result = policy().check_policy({'patent_type': 'invention', 'application_stage': 'filed',
        'technical_types': ['mechanical'], 'views': [{'type': 'section', 'new_feature': True}]})
    assert any(x['rule'] == 'CN-NO-ADDED-MATTER' and x['verdict'] == 'FAIL' for x in result)
