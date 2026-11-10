"""Validated operator reference preparation, not reconstruction of hidden facts."""
import datetime
import hashlib
import io
from PIL import Image
from .images import prepare_reference, prepare_mask


def validate_preparation(prep: dict, source_record: dict, image: bytes, mask: bytes | None) -> dict:
    ref = prepare_reference(image)
    for key in ['source_sha256', 'source_region_id', 'base_sha256']:
        if prep.get(key) != source_record.get(key): raise ValueError('reference preparation source/base identity differs')
    if prep.get('base_sha256') != hashlib.sha256(image).hexdigest(): raise ValueError('reference base hash differs')
    if prep.get('method') not in {'crop', 'mask'} or prep.get('orientation') != 'normalized':
        raise ValueError('reference preparation method/orientation invalid')
    if prep.get('size') != [ref.width, ref.height]: raise ValueError('reference normalized dimensions differ')
    if not all(isinstance(prep.get(k), str) and prep[k].strip() for k in ['reviewer', 'source_note']):
        raise ValueError('reference preparation requires reviewer and source note')
    try:
        if datetime.datetime.fromisoformat(prep['reviewed_at']).tzinfo is None: raise ValueError('timezone required')
    except (KeyError, TypeError, ValueError): raise ValueError('reference preparation timestamp invalid') from None
    regions = prep.get('allowed_regions', [])
    if not isinstance(regions, list) or not 1 <= len(regions) <= 24: raise ValueError('allowed reference regions required')
    for r in regions:
        if (not isinstance(r, list) or len(r) != 4 or any(type(x) is not int for x in r)
                or not 0 <= r[0] < r[2] <= ref.width or not 0 <= r[1] < r[3] <= ref.height):
            raise ValueError('reference region outside normalized image')
    if prep['method'] == 'mask':
        if mask is None or prep.get('mask_sha256') != hashlib.sha256(mask).hexdigest():
            raise ValueError('reference mask identity differs')
        try: dispatched_mask = prepare_mask(mask, ref)
        except Exception as error: raise ValueError('invalid reference mask') from error
        with Image.open(io.BytesIO(dispatched_mask.blob)) as im:
            alpha = im.convert('RGBA').getchannel('A')
            allowed = Image.new('1', alpha.size, 0)
            from PIL import ImageDraw
            draw = ImageDraw.Draw(allowed)
            for x1, y1, x2, y2 in regions: draw.rectangle([x1, y1, x2-1, y2-1], fill=1)
            if any(a == 0 and not ok for a, ok in zip(alpha.tobytes(), allowed.convert('L').tobytes())):
                raise ValueError('transparent mask pixels outside reviewed regions')
    elif mask is not None: raise ValueError('crop preparation cannot carry a mask')
    result = dict(prep)
    if mask is not None: result['dispatch_mask_sha256'] = dispatched_mask.sha256
    return result
