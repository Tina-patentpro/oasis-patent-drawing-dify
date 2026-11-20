from hashlib import sha256
from io import BytesIO

import pytest
from PIL import Image, ImageDraw

from patent_drawing.errors import DrawingError
from patent_drawing.reference_preparation import compose_reviewed_mask, validate_preparation
from patent_drawing.operation_store import digest
from patent_drawing.work_queue import WorkActor, WorkConfig


def png(image, **options):
    buffer = BytesIO()
    image.save(buffer, format='PNG', **options)
    return buffer.getvalue()


def test_approved_mask_restores_every_protected_pixel_and_keeps_raw_change_count():
    ref = png(Image.new('RGB', (16, 12), 'white'))
    output = png(Image.new('RGB', (16, 12), 'black'))
    mask = Image.new('RGBA', (16, 12), 'white')
    ImageDraw.Draw(mask).rectangle((4, 6, 7, 10), fill=(0, 0, 0, 0))
    merged, report = compose_reviewed_mask(ref, png(mask), output)
    with Image.open(BytesIO(merged)) as image:
        assert image.getpixel((3, 6)) == (255, 255, 255)
        assert image.getpixel((4, 6)) == image.getpixel((7, 10)) == (0, 0, 0)
        assert image.getpixel((8, 10)) == (255, 255, 255)
    assert report == {'raw_changed_outside': 172, 'delivery_changed_outside': 0, 'changed_inside': 20}
    with pytest.raises(DrawingError, match='尺寸'):
        compose_reviewed_mask(ref, png(mask), png(Image.new('RGB', (12, 16), 'black')))


def test_reference_approval_rejects_swapped_or_expanded_mask():
    ref = png(Image.new('RGB', (16, 12), 'white'))
    source = {'source_sha256': 'b' * 64, 'source_region_id': 'f2', 'base_sha256': sha256(ref).hexdigest()}
    prep = {**source, 'method': 'mask', 'orientation': 'normalized', 'size': [16, 12],
            'reviewer': 'fixture', 'source_note': 'visible segment', 'reviewed_at': '2026-11-15T00:00:00+08:00',
            'allowed_regions': [[4, 6, 8, 11]]}
    mask = png(Image.new('RGBA', (16, 12), (0, 0, 0, 0)))
    with pytest.raises(ValueError, match='outside reviewed'):
        validate_preparation({**prep, 'mask_sha256': sha256(mask).hexdigest()}, source, ref, mask)
    inverted = Image.new('RGBA', (16, 12), 'white')
    ImageDraw.Draw(inverted).rectangle((4, 6, 7, 10), fill=(0, 0, 0, 0))
    exif = Image.Exif()
    exif[274] = 2
    flipped = png(inverted, exif=exif)
    with pytest.raises(ValueError, match='orientation'):
        validate_preparation({**prep, 'mask_sha256': sha256(flipped).hexdigest()}, source, ref, flipped)


def test_scoped_comparison_authority_requires_matching_run_actor_source_and_revision(monkeypatch):
    actor = WorkActor('a' * 32, 'reviewer', 'comparison-2')
    state = {'source_sha256': 'b' * 64, 'proposal': {'revision': 1, 'mask': 'fixture'}}
    scope = {'user': actor.user, 'conversation': actor.conversation, 'workspace_id': 'workspace',
             'source_sha256': state['source_sha256'], 'proposal_sha256': digest(state['proposal']),
             'intake_revision': 1, 'run_id': 'ab-2', 'expires_at': 150}
    config = WorkConfig('isolated-app', {}, comparison_scope=scope)
    monkeypatch.setattr('patent_drawing.work_queue.time.time', lambda: 100)
    config.check_comparison_scope(actor, 'workspace', state, 1, run_id='ab-2')
    for user, current, revision, run in (
        (WorkActor(actor.tenant, actor.user, 'another'), state, 1, 'ab-2'),
        (actor, {**state, 'proposal': {'revision': 1, 'mask': 'swapped'}}, 1, 'ab-2'),
        (actor, state, 2, 'ab-2'),
        (actor, state, 1, 'ab-1'),
    ):
        with pytest.raises(ValueError, match='授权'):
            config.check_comparison_scope(user, 'workspace', current, revision, run_id=run)
    monkeypatch.setattr('patent_drawing.work_queue.time.time', lambda: 150)
    with pytest.raises(ValueError, match='授权'):
        config.check_comparison_scope(actor, 'workspace', state, 1, run_id='ab-2')
