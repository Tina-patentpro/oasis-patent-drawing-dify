"""Shared explicit bounds -> deterministic orthographic schematic views.

Bounding-box illustrations do not claim source shape reconstruction or CAD
precision. Spatial candidates and supplied source images use separate paths.
"""
import hashlib
import io
from PIL import Image, ImageDraw
from .operation_store import digest
from .scene import validate_scene


def render_view(scene, view):
    problems = validate_scene(scene)
    if any(p['verdict'] == 'FAIL' for p in problems): raise ValueError('scene geometric relations fail')
    projection = view.get('projection')
    if projection not in {'front', 'top'}: raise ValueError('unsupported schematic projection')
    width, height = view.get('width', 1200), view.get('height', 900)
    if any(type(x) is not int or not 400 <= x <= 2400 for x in [width, height]): raise ValueError('view canvas capacity')
    axes = (0, 2) if projection == 'front' else (0, 1)
    rows = scene['instances']
    if not rows: raise ValueError('no explicit geometry')
    lo = [min(r['bounds'][i] for r in rows) for i in axes]
    hi = [max(r['bounds'][i + 3] for r in rows) for i in axes]
    scale = min((width - 100) / (hi[0] - lo[0]), (height - 100) / (hi[1] - lo[1]))
    image = Image.new('RGB', (width, height), 'white'); draw = ImageDraw.Draw(image); anchors = {}
    for row in rows:
        b = row['bounds']; x1 = 50 + (b[axes[0]] - lo[0]) * scale; x2 = 50 + (b[axes[0] + 3] - lo[0]) * scale
        y1 = height - 50 - (b[axes[1] + 3] - lo[1]) * scale; y2 = height - 50 - (b[axes[1]] - lo[1]) * scale
        draw.rectangle([x1, y1, x2, y2], outline='black', width=3)
        anchors[row['id']] = [x1, y1, x2, y2]
    out = io.BytesIO(); image.save(out, format='PNG')
    return {'png': out.getvalue(), 'scene_sha256': digest(scene), 'instance_ids': [x['id'] for x in rows],
        'instance_regions': anchors, 'geometry_status': 'explicit_bounds_schematic',
        'technical_shape_accuracy': 'not_verified', 'projection': projection}
