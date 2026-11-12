#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Self-test for ipv_canvas (numpy + Pillow). Run: python tests/test_canvas.py"""
import io
import json
import os
import struct
import sys
import tempfile

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ipv_canvas as C

rng = np.random.default_rng(7)


def make_canvas(w, h):
    a = np.zeros((h, w, 4), np.uint8)
    a[10:40, 15:50] = [200, 30, 30, 255]
    a[20:30, 25:35, 3] = 0                     # дырка: RGB есть, alpha=0
    a[5, 5] = [0, 255, 0, 128]
    a[rng.random((h, w)) < 0.02] = [30, 30, 200, 200]
    return a


def norm(x):
    # RPNG не хранит RGB под alpha=0 — сравниваем видимые пиксели
    b = x.copy()
    b[b[:, :, 3] == 0] = 0
    return b


def main():
    for w, h in [(64, 48), (17, 9)]:
        a = make_canvas(w, h)
        blob = C.rpng_encode(a, flag=4, z2=0xFFFFFF00)
        assert np.array_equal(norm(C.rpng_decode(blob)['canvas']), norm(a))
        ok, _ = C.rpng_selfcheck(blob)
        assert ok
        print(f'PASS  RPNG {w}x{h}')
    a = np.full((260, 260, 4), [10, 20, 30, 255], np.uint8)   # run 67600 > 65535
    assert np.array_equal(C.rpng_decode(C.rpng_encode(a))['canvas'], a)
    print('PASS  RPNG solid 260x260 (run splitting)')
    assert C.rpng_encode(np.zeros((10, 10, 4), np.uint8)) == b''
    print('PASS  empty layer -> len 0')

    F = C.make_frame
    ts = 1.7e9
    cA = make_canvas(64, 48)
    cB = np.zeros((48, 64, 4), np.uint8); cB[30:45, 5:60] = [0, 0, 255, 255]
    blobA, blobB = C.rpng_encode(cA), C.rpng_encode(cB)
    payA = struct.pack('>diiHI', ts, 1, 1, 0x0400, len(blobA)) + blobA
    payB = struct.pack('>diiHI', ts, 2, 1, 0x0400, len(blobB)) + blobB
    buf = io.BytesIO(); Image.fromarray(C.composite([cA, cB], 64, 48), 'RGBA').save(buf, 'PNG')
    prev_bytes = buf.getvalue()
    payP = struct.pack('>diiHI', ts, -1, 0, 0, len(prev_bytes)) + prev_bytes
    canvas_pay = struct.pack('>dIIH', ts, 64, 48, 4) + b'TEST' + b'\x00'
    data = b''.join([F(C.T_CANVAS, canvas_pay), b'\x00\x00', F(C.T_RASTER, payA),
                     F(C.T_RASTER, payB), F(0x01000600, b'footer' * 8),
                     F(C.T_RASTER, payP), b'\xff'])
    orig = C.split_items(data)

    with tempfile.TemporaryDirectory() as td:
        src = os.path.join(td, 't.ipv'); open(src, 'wb').write(data)
        out = os.path.join(td, 'ex')
        C.cmd_extract(src, out)
        gotA = np.asarray(Image.open(os.path.join(out, 'layers', 'layer_1.png')).convert('RGBA'))
        assert np.array_equal(norm(gotA), norm(cA))
        trim = np.asarray(Image.open(os.path.join(out, 'layers_trimmed',
                                                   'layer_2_x5_y30.png')).convert('RGBA'))
        assert np.array_equal(trim, cB[30:45, 5:60])
        man = json.load(open(os.path.join(out, 'manifest.json'), encoding='utf-8'))
        assert man['canvas'] == {'width': 64, 'height': 48}
        assert man['layers'][1]['bbox'] == {'x': 5, 'y': 30, 'w': 55, 'h': 15}
        flat = np.asarray(Image.open(os.path.join(out, 'flattened.png')).convert('RGBA'))
        assert np.array_equal(flat, C.composite([cA, cB], 64, 48))
        assert open(os.path.join(out, 'preview.png'), 'rb').read() == prev_bytes
        print('PASS  extract: layers/trimmed/manifest/flattened/preview')

        newB = np.zeros((48, 64, 4), np.uint8); newB[0:10, 0:10] = [255, 255, 0, 255]
        npng = os.path.join(td, 'newB.png'); C.save_png(newB, npng)
        dst = os.path.join(td, 't2.ipv')
        C.cmd_replace(src, 2, npng, dst)
        _, items2, ras2, _ = C.load_ipv(dst)
        for idx in [0, 1, 2, 4, 6]:
            assert orig[idx] == items2[idx], f'item {idx}'
        assert np.array_equal(next(r for r in ras2 if r['lid'] == 2)['dec']['canvas'], newB)
        prev = next(r for r in ras2 if r['kind'] == 0)
        assert np.array_equal(np.asarray(Image.open(io.BytesIO(prev['data'])).convert('RGBA')),
                              C.composite([cA, newB], 64, 48))
        print('PASS  replace: untouched byte-exact, pixels, preview')

        cA2 = np.zeros((48, 64, 4), np.uint8); cA2[5:20, 10:30] = [0, 200, 0, 255]
        batch = os.path.join(td, 'batch'); os.makedirs(batch)
        C.save_png(cA2, os.path.join(batch, 'layer_1.png'))
        C.save_png(newB[0:10, 0:10], os.path.join(batch, 'layer_2_x0_y0.png'))
        dst2 = os.path.join(td, 't3.ipv')
        C.cmd_replace_many(src, batch, dst2)
        _, items3, ras3, _ = C.load_ipv(dst2)
        for idx in [0, 1, 4, 6]:
            assert orig[idx] == items3[idx], f'item {idx}'
        assert np.array_equal(next(r for r in ras3 if r['lid'] == 1)['dec']['canvas'], cA2)
        assert np.array_equal(next(r for r in ras3 if r['lid'] == 2)['dec']['canvas'], newB)
        prev3 = next(r for r in ras3 if r['kind'] == 0)
        assert np.array_equal(np.asarray(Image.open(io.BytesIO(prev3['data'])).convert('RGBA')),
                              C.composite([cA2, newB], 64, 48))
        print('PASS  replace-many: full + cropped')
    print('ALL PASS')


if __name__ == '__main__':
    main()
