"""Fixed-canvas endpoint closure using a common reference and registered morph.

Only head/tail windows participate. Equal endpoints are a pixel guarantee, not
a claim that motion or optical-flow correspondence is perceptually correct.
"""
from collections import OrderedDict
import hashlib
import math
import threading

import numpy as np
from PIL import Image

_CACHE = OrderedDict()
_LOCK = threading.RLock()
_MAX_CACHE_BYTES = 64 * 1024 * 1024
_FLOW_LONG_SIDE = 640
_MAX_PIXELS = 8_000_000


def active(value):
    return isinstance(value, dict) and value.get('enabled') is True


def _integer(value, label):
    if (isinstance(value, bool) or not isinstance(value, (int, float)) or
            not math.isfinite(value) or int(value) != value):
        raise ValueError(f'{label}必須為整數')
    return int(value)


def validate(value, start, end, dimensions=None):
    if not isinstance(value, dict) or any(key not in ('enabled', 'reference', 'head_frames', 'tail_frames') for key in value):
        raise ValueError('首尾共同基準設定格式無效')
    enabled = value.get('enabled', False)
    if not isinstance(enabled, bool):
        raise ValueError('首尾共同基準開關必須為布林值')
    length = end - start + 1
    default_count = max(2, min(12, length // 2))
    out = dict(enabled=enabled,
               reference=_integer(value.get('reference', start), '共同基準幀'),
               head_frames=_integer(value.get('head_frames', default_count), '開頭漸變幀數'),
               tail_frames=_integer(value.get('tail_frames', default_count), '結尾漸變幀數'))
    if enabled and not start <= out['reference'] <= end:
        raise ValueError('共同基準幀必須在循環範圍內')
    if enabled and min(out['head_frames'], out['tail_frames']) < 2:
        raise ValueError('首尾漸變各至少需要兩幀，包含端點與零修正的內側幀')
    if enabled and (length < 4 or out['head_frames'] + out['tail_frames'] > length):
        raise ValueError('首尾漸變範圍不能重疊；請縮短漸變幀數或增加循環範圍')
    if enabled and dimensions is not None:
        _validate_dimensions(*dimensions)
    return out


def _validate_dimensions(w, h):
    if w * h > _MAX_PIXELS or min(w, h) < 8 or max(w, h) > 4096:
        raise ValueError('首尾漸變支援每邊 8–4096 像素、最多 800 萬像素的畫布')


def weight(recipe, index):
    value = recipe.get('closure')
    if not active(value) or not recipe['start'] <= index <= recipe['end']:
        return 0.
    head = 1 - (index - recipe['start']) / (value['head_frames'] - 1)
    tail = 1 - (recipe['end'] - index) / (value['tail_frames'] - 1)
    t = min(1., max(0., head, tail))
    return t * t * (3 - 2 * t)


def clear_cache():
    with _LOCK:
        _CACHE.clear()


def _cv():
    try:
        import cv2
    except (ImportError, OSError) as exc:
        raise ValueError('首尾共同基準需要 OpenCV，請執行 setup.cmd 安裝依賴') from exc
    return cv2


def _associated(pixels):
    value = np.asarray(pixels, dtype=np.float32).copy()
    value[:, :, :3] *= value[:, :, 3:4] / 255.
    return value


def _gray(cv, pixels, size):
    gray = cv.cvtColor(pixels[:, :, :3], cv.COLOR_RGB2GRAY).astype(np.float32)
    alpha = pixels[:, :, 3].astype(np.float32) / 255.
    gray *= alpha
    gray += 64 * (1 - alpha)
    gray = np.uint8(np.clip(np.rint(gray), 0, 255))
    return cv.resize(gray, size, interpolation=cv.INTER_AREA) if gray.shape[::-1] != size else gray


def _flows(source, target):
    """Cache only reduced flow, keyed by actual pixels, within a fixed byte budget."""
    cv = _cv()
    h, w = source.shape[:2]
    digest = hashlib.sha256()
    digest.update(str(source.shape).encode())
    digest.update(source.tobytes())
    digest.update(target.tobytes())
    key = digest.digest()
    with _LOCK:
        cached = _CACHE.get(key)
        if cached is not None:
            _CACHE.move_to_end(key)
            return cached
        scale = min(1., _FLOW_LONG_SIDE / max(w, h))
        size = (max(8, round(w * scale)), max(8, round(h * scale)))
        a, b = _gray(cv, source, size), _gray(cv, target, size)
        options = dict(pyr_scale=.5, levels=5, winsize=31, iterations=5, poly_n=7, poly_sigma=1.5, flags=0)
        forward = cv.calcOpticalFlowFarneback(a, b, None, **options)
        backward = cv.calcOpticalFlowFarneback(b, a, None, **options)
        # Bound pathological estimates. This tool is for close poses; it cannot
        # synthesize missing anatomy or rescue a completely different pose.
        limit = max(4., min(100., min(w, h) * .18) * scale)
        forward = np.clip(np.nan_to_num(forward), -limit, limit)
        backward = np.clip(np.nan_to_num(backward), -limit, limit)
        cached = (forward, backward)
        _CACHE[key] = cached
        while sum(a.nbytes + b.nbytes for a, b in _CACHE.values()) > _MAX_CACHE_BYTES:
            _CACHE.popitem(last=False)
        return cached


def _full_flow(cv, flow, w, h):
    sh, sw = flow.shape[:2]
    result = cv.resize(flow, (w, h), interpolation=cv.INTER_LINEAR)
    result[:, :, 0] *= w / sw
    result[:, :, 1] *= h / sh
    # Pin normal motion at canvas boundaries. A cropped character must not gain
    # an invented transparent strip just because the registration shrinks it.
    # This preserves the canvas, but cannot reconstruct content outside it.
    yy, xx = np.mgrid[:h, :w].astype(np.float32)
    width = max(2., min(16., min(w, h) * .06))
    for channel, distance in ((0, np.minimum(xx, w - 1 - xx)),
                              (1, np.minimum(yy, h - 1 - yy))):
        influence = np.clip(distance / width, 0, 1)
        result[:, :, channel] *= influence * influence * (3 - 2 * influence)
    return result


def _inverse_positions(cv, flow, fraction, xx, yy):
    # Solve destination = source + fraction * forward_flow(source). Merely
    # subtracting flow(destination) double-edges at spatially varying motion.
    u, v = xx.copy(), yy.copy()
    for _ in range(4):
        sampled = cv.remap(flow, u, v, cv.INTER_LINEAR, borderMode=cv.BORDER_REPLICATE)
        u = xx - fraction * sampled[:, :, 0]
        v = yy - fraction * sampled[:, :, 1]
        # Bound the inverse map; edge pixels may stretch with a large mismatch,
        # which is preferable to introducing an artificial empty canvas border.
        u = np.clip(u, 0, flow.shape[1] - 1)
        v = np.clip(v, 0, flow.shape[0] - 1)
    return u, v


def morph(image, reference, amount):
    if image.size != reference.size:
        raise ValueError('共同基準與影格的畫布尺寸不同')
    if amount <= 0:
        return image.copy()
    if amount >= 1:
        return reference.copy()  # Includes invisible RGB and every alpha value.
    source = np.asarray(image.convert('RGBA'))
    target = np.asarray(reference.convert('RGBA'))
    if np.array_equal(source, target):
        return image.copy()
    h, w = source.shape[:2]
    _validate_dimensions(w, h)
    cv = _cv()
    forward, backward = _flows(source, target)
    yy, xx = np.mgrid[:h, :w].astype(np.float32)
    u, v = _inverse_positions(cv, _full_flow(cv, forward, w, h), amount, xx, yy)
    a = cv.remap(_associated(source), u, v, cv.INTER_LINEAR, borderMode=cv.BORDER_CONSTANT, borderValue=0)
    u, v = _inverse_positions(cv, _full_flow(cv, backward, w, h), 1 - amount, xx, yy)
    b = cv.remap(_associated(target), u, v, cv.INTER_LINEAR, borderMode=cv.BORDER_CONSTANT, borderValue=0)
    a *= 1 - amount
    b *= amount
    a += b
    del b
    out = a
    alpha = out[:, :, 3:4]
    out[:, :, :3] = np.divide(out[:, :, :3] * 255, alpha, out=np.zeros_like(out[:, :, :3]), where=alpha > 0)
    out = np.uint8(np.clip(np.rint(out), 0, 255))
    out[out[:, :, 3] == 0, :3] = 0
    return Image.fromarray(out, 'RGBA')


def verify(folder, count, value):
    """Read the actual delivered PNGs; adjacent change is distinct from equality."""
    def read(index):
        with Image.open(folder / f'frame_{index:08d}.png') as image:
            return np.array(image.convert('RGBA'))
    first, last = read(1), read(count)
    if first.shape != last.shape:
        raise ValueError('輸出的首尾 PNG 尺寸不同')
    error = np.abs(first.astype(np.int16) - last.astype(np.int16))
    def delta(a, b):
        return round(float(np.mean(np.abs(_associated(a) - _associated(b)))), 6)
    return dict(enabled=True, reference=value['reference'], head_frames=value['head_frames'],
                tail_frames=value['tail_frames'], endpoints_equal=bool(not np.any(error)),
                max_channel_error=int(error.max()), changed_pixels=int(np.any(error, axis=2).sum()),
                head_adjacent_delta=delta(first, read(2)), tail_adjacent_delta=delta(read(count - 1), last),
                delta_units='mean absolute premultiplied RGBA channel difference, 0-255')
