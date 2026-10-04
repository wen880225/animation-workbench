"""Conservative fixed-per-clip tone matching from opaque foreground samples.

This module never estimates per-frame corrections: one bounded curve is reused
for the whole clip, preserving alpha, canvas dimensions, pure black and white.
"""
import numpy as np

QUANTILES = np.array([.10, .25, .50, .75, .90])
NOTE = ('以不透明前景抽樣估計，每段使用固定明暗曲線；純黑、純白與透明度保持。'
        '姿勢、服裝或光源差異會影響估計，請播放檢查；此功能不會消除片段內逐幀閃爍。')


def curve(values, tone=0, contrast=0):
    x = np.asarray(values, dtype=np.float64) / 255
    x = x ** (2 ** (-tone / 100))
    power = 2 ** (contrast / 50)
    a, b = x ** power, (1 - x) ** power
    return 255 * a / (a + b)


def sample_pixels(image, limit=18000):
    pixels = np.asarray(image.convert('RGBA'))
    rgb = pixels[:, :, :3]
    luma = rgb.astype(np.float64) @ np.array([.2126, .7152, .0722])
    # Do not let invisible RGB, alpha fringes or flat black/white dominate a fit.
    mask = (pixels[:, :, 3] >= 224) & (luma >= 4) & (luma <= 251)
    selected = rgb[mask]
    if len(selected) > limit:
        selected = selected[np.linspace(0, len(selected) - 1, limit).astype(int)]
    return selected


def distribution(samples, tone=0, contrast=0):
    if not samples or not any(len(p) for p in samples):
        raise ValueError('沒有足夠的不透明中間調前景可供明暗校準；透明、純黑或純白不列入估計')
    # Each sampled frame contributes equally, regardless of foreground area.
    per_frame = []
    for pixels in samples:
        if len(pixels):
            rgb = np.rint(curve(pixels, tone, contrast))
            per_frame.append(np.quantile(rgb @ np.array([.2126, .7152, .0722]), QUANTILES))
    return np.median(per_frame, axis=0)


def summary(q, target):
    return dict(median=round(float(q[2]), 3), spread=round(float(q[4] - q[0]), 3),
                error=round(float(np.mean(np.abs(q - target))), 3))


def fit(samples, target):
    before = distribution(samples)
    warnings = []
    pixel_count = sum(len(p) for p in samples)
    if pixel_count < 256:
        warnings.append('中間調前景抽樣偏少，請人工確認')
    if before[4] - before[0] < 12 or target[4] - target[0] < 12:
        warnings.append('明暗層次不足，已保留原設定；請手動調整')
        return dict(tone=0.0, contrast=0.0, before=summary(before, target),
                    after=summary(before, target), warnings=warnings, samples=pixel_count)
    # Fitting luminance quantiles is deliberately bounded, then measured again
    # against RGB samples using the actual per-channel curve.
    best = (float(np.mean((before - target) ** 2)), 0., 0.)
    for step, radius in [(5., None), (1., 5.), (.25, 1.)]:
        tones = np.arange(-60, 60.01, step) if radius is None else np.arange(max(-60, best[1]-radius), min(60, best[1]+radius)+step/2, step)
        contrasts = np.arange(-35, 35.01, step) if radius is None else np.arange(max(-35, best[2]-radius), min(35, best[2]+radius)+step/2, step)
        for tone in tones:
            for contrast in contrasts:
                error = float(np.mean((curve(before, tone, contrast) - target) ** 2))
                # Prefer a smaller adjustment when two fits are indistinguishable.
                score = error + .00002 * (tone*tone + contrast*contrast)
                if score < best[0]:
                    best = score, float(tone), float(contrast)
    tone, contrast = best[1:]
    after = distribution(samples, tone, contrast)
    if np.mean(np.abs(after-target)) > np.mean(np.abs(before-target)):
        tone = contrast = 0.
        after = before.copy()
        warnings.append('此素材的自動曲線未改善差異，已保留原設定')
    if abs(tone) >= 59.75 or abs(contrast) >= 34.75:
        warnings.append('調整已達保守上限，素材差異可能超出全局明暗可處理的範圍')
    if np.mean(np.abs(after-target)) > 5:
        warnings.append('校準後仍有分布差異；姿勢、服裝或局部光照可能不同，請人工確認')
    return dict(tone=round(tone, 2), contrast=round(contrast, 2), before=summary(before, target),
                after=summary(after, target), warnings=warnings, samples=pixel_count)
