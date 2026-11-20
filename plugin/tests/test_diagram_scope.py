from io import BytesIO

from PIL import Image

from patent_drawing.diagrams import render_diagram


def test_container_outline_preserves_parent_to_leaf_links_in_final_pixels():
    # A closed scope with no fill can be layered over graph nodes and edges.
    # Here the old default white scope hid both valid edges and their arrowheads.
    plan = {'parts': [{'id': 'parent', 'name': '父节点', 'label': ''}]}
    view = {'diagram': {
        'width': 900, 'height': 650,
        'nodes': [
            {'id': 'parent', 'type': 'process', 'box': [325, 130, 575, 210], 'text': '父节点'},
            {'id': 'left', 'type': 'process', 'box': [100, 420, 360, 500], 'text': '目标叶节点'},
            {'id': 'right', 'type': 'process', 'box': [540, 420, 800, 500], 'text': '兄弟叶节点'}
        ],
        'edges': [
            {'source': 'parent', 'target': 'left', 'points': [[370, 210], [230, 275], [230, 420]]},
            {'source': 'parent', 'target': 'right', 'points': [[530, 210], [670, 275], [670, 420]]}
        ],
        'primitives': [{'type': 'rect', 'id': 'scope', 'box': [60, 70, 840, 550], 'part': 'parent'}]
    }, 'parts': ['parent']}
    output = render_diagram(plan, view)
    with Image.open(BytesIO(output['png'])) as image:
        assert image.getpixel((230, 330)) == (0, 0, 0)
        assert image.getpixel((670, 330)) == (0, 0, 0)
        assert image.getpixel((230, 413)) == (0, 0, 0)
        assert image.getpixel((670, 413)) == (0, 0, 0)
    assert b'width="780" height="480" fill="none"' in output['svg']
