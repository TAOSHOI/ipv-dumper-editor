#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Self-test for ipv_dumper (stdlib only). Run: python tests/test_roundtrip.py"""
import base64
import json
import os
import struct
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ipv_dumper as D

PNG = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAYAAADED76LAAAAFklEQVR4nGM8ISf3nwEP'
    'YMInOXwUAAAXJAITh6nqIwAAAABJRU5ErkJggg==')


def build_sample():
    ts = 1.7e9
    F = D.make_frame
    canvas = struct.pack('>dIIH', ts, 512, 512, 9) + b'TEST_0001' + b'\x00'
    session = struct.pack('>I', 0x10527) + struct.pack('>d', ts) + bytes(40)

    def point(t, x, y, extra=0.8):
        return F(D.T_POINT, struct.pack('>dff', t, x, y) + struct.pack('>f', extra))

    brush = bytearray(292)
    struct.pack_into('>f', brush, 12, 10.0)      # гипотеза размера: f32 на +12
    tail = F(0x01000202, bytes(brush)) + F(0x02000303, bytes(30))
    stroke = (struct.pack('>dd6I', ts + 1, ts + 1.4, 0, 1, 0x7000, 0x29253bff, 1, 3)
              + point(ts + 1, 10, 10) + point(ts + 1.2, 50, 60, 0.7)
              + point(ts + 1.4, 90, 20, 0.6) + tail)
    # ловушка: NaN с нестандартными битами — обязан упасть в raw
    nan_pt = F(D.T_POINT, struct.pack('>dff', ts + 2, 1, 2) + struct.pack('>I', 0x7fc01234))
    nan_stroke = struct.pack('>dd6I', ts + 2, ts + 2.1, 0, 1, 0x7000, 0xff0000ff, 1, 1) + nan_pt + tail
    fill = (struct.pack('>ddI', ts + 3, ts + 3.05, 1) + point(ts + 3, 100, 100, 0.0)
            + struct.pack('>II', 0x00ff00ff, 1) + point(ts + 3, 100, 100, 0.0)
            + struct.pack('>If', 0, 0.5) + struct.pack('>d', ts + 3.05)
            + struct.pack('>I', 1) + bytes(8))
    ras_prev = struct.pack('>diiHI', ts + 4, -1, 0, 0, len(PNG)) + PNG
    rpng = (b'RPNG' + struct.pack('>II', 45, 0) + bytes(45) + struct.pack('>I', 0)
            + struct.pack('>I', len(PNG)) + PNG)
    ras_layer = struct.pack('>diiHI', ts + 4, 1, 1, 0x0400, len(rpng)) + rpng
    footer = struct.pack('>I', 0x527) + bytes(64)
    data = b''.join([
        F(D.T_CANVAS, canvas), F(D.T_SESSION, session),
        b'\x00\x00',                              # щель между кадрами
        F(D.T_STROKE, stroke), F(D.T_STROKE, nan_stroke), F(D.T_FILL, fill),
        F(0x02000C00, b'\xde\xad\xbe\xef' * 4),   # неизвестный кадр
        F(D.T_RASTER, ras_layer), F(D.T_RASTER, ras_prev),
        F(D.T_FOOTER, footer), b'\xff\xff',       # мусор в конце файла
    ])
    return data, ras_layer


def main():
    data, ras_layer = build_sample()
    r = D.verify_bytes(data)
    assert r['ok'], 'round-trip not byte-exact'
    assert r['gaps'] == 2, 'gaps lost'
    assert r['fallbacks'].get('0x02000300') == 1, 'NaN stroke did not fall back to raw'
    print('PASS  round-trip: byte-exact, gaps preserved, NaN stroke -> raw')

    with tempfile.TemporaryDirectory() as td:
        ipv = os.path.join(td, 'sample.ipv')
        with open(ipv, 'wb') as f:
            f.write(data)
        lib = D.collect_presets([ipv])
        assert len(lib['presets']) == 1, 'unverified stroke leaked into presets'
        pr = next(iter(lib['presets'].values()))
        assert pr['size_hint_brush12'] == 10.0, 'brush12 hint wrong'
        print('PASS  presets: roundtrip-filtered, size_hint_brush12 = 10.0')

        base = os.path.join(td, 'base.json')
        with open(base, 'w', encoding='utf-8') as f:
            json.dump(D.dump_bytes(data, 'sample.ipv'), f, ensure_ascii=False)
        thumb = os.path.join(td, 'thumb.png')
        thumb_bytes = PNG + b'\x00\x00'   # отличные байты; мусор после IEND декодеры игнорируют
        with open(thumb, 'wb') as f:
            f.write(thumb_bytes)
        instr = {'base': 'base.json', 'thumbnail': 'thumb.png', 'strokes': [
            {'layer': 1, 'color': '#29253b',
             'points': [[10, 10], [200, 150, 0.5], [300, 80]], 'dur': 0.5}]}
        built = D.build_bytes(D.compose(instr, base_dir=td))
        ok_prev = ok_layer = False
        strokes = []
        for it in D.split_items(built):
            if it[0] != 'frame':
                continue
            if it[1] == D.T_RASTER:
                lid, kind = struct.unpack_from('>ii', it[2], 8)
                if (lid, kind) == (-1, 0):
                    ok_prev = it[2].endswith(thumb_bytes)
                if (lid, kind) == (1, 1):
                    ok_layer = it[2] == ras_layer
            if it[1] == D.T_STROKE:
                strokes.append(it)
        assert ok_prev, 'thumbnail did not reach the preview'
        assert ok_layer, 'layer RPNG was corrupted'
        assert len(strokes) == 1 and len(D.parse_stroke(strokes[0][2])['points']) == 3
        print('PASS  compose: stroke injected, thumbnail -> preview, layer RPNG intact')
    print('ALL PASS')


if __name__ == '__main__':
    main()
