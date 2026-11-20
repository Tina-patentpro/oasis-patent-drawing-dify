from io import BytesIO
from xml.etree import ElementTree as ET

import pytest
from PIL import Image

from patent_drawing.diagrams import render_diagram
from patent_drawing.errors import DrawingError


@pytest.fixture
def flow_view():
    return {
        'parts': [],
        'diagram': {
            'width': 1600,
            'height': 1800,
            'nodes': [
                {'id': 'start', 'type': 'terminal', 'box': [450, 40, 1050, 110],
                 'text': '设备记录任务\n可离线编辑'},
                {'id': 'next', 'type': 'process', 'box': [450, 170, 1050, 260],
                 'text': '保存记录'},
            ],
            'edges': [
                {'source': 'start', 'target': 'next', 'points': [[750, 110], [750, 170]]},
            ],
        },
    }


def test_two_lines_fit_short_terminal_without_losing_text_or_edge(flow_view):
    rendered = render_diagram({'parts': []}, flow_view)
    svg = ET.fromstring(rendered['svg'])
    nodes = svg.findall('{*}text')
    assert [node.text for node in nodes] == ['设备记录任务', '可离线编辑', '保存记录']
    assert all(18 <= int(node.get('font-size')) <= 28 for node in nodes[:2])
    assert svg.find('{*}polyline').get('points') == '750,110 750,170'

    with Image.open(BytesIO(rendered['png'])) as image:
        for node in nodes[:2]:
            x = int(float(node.get('x')))
            size = int(node.get('font-size'))
            y = int(float(node.get('y'))) - size
            assert 40 < y < y + size < 110
            assert image.crop((x, y, 850, y + size)).getextrema() == ((0, 255),) * 3


def test_node_text_still_fails_when_the_minimum_readable_layout_cannot_fit(flow_view):
    flow_view['diagram']['nodes'][0]['text'] += '\n第三行\n第四行'
    with pytest.raises(DrawingError, match='不能在节点中完整排版'):
        render_diagram({'parts': []}, flow_view)


def test_vertical_ellipsis_is_three_dots_in_svg_and_png(flow_view):
    flow_view['diagram']['primitives'] = [
        {'type': 'text', 'text': '⋮', 'at': [300, 40], 'font_size': 26, 'anchor': 'middle'},
    ]
    rendered = render_diagram({'parts': []}, flow_view, layered=True)
    svg = ET.fromstring(rendered['svg'])
    dots = svg.findall('{*}g/{*}circle')
    assert len(dots) == 3
    assert {float(dot.get('cx')) for dot in dots} == {300}
    ys = [float(dot.get('cy')) for dot in dots]
    assert ys[1] - ys[0] == ys[2] - ys[1] > 3
    with Image.open(BytesIO(rendered['png'])) as image:
        assert all(image.getpixel((300, round(y))) == (0, 0, 0) for y in ys)
        assert image.getpixel((300, round((ys[0] + ys[1]) / 2))) == (255, 255, 255)


def test_unsupported_drawing_glyph_blocks_before_export(flow_view):
    flow_view['diagram']['nodes'][0]['text'] = '限制\U0010ffff'
    with pytest.raises(DrawingError, match=r'缺少字形.*U\+10FFFF'):
        render_diagram({'parts': []}, flow_view)
