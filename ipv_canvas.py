#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ipv_canvas v0.2 — редактор холста .ipv (фулл-Python): слои в PNG, обратно, превью.

Факты формата (RPNG, записи (1,5)) — из IPV_FORMAT.md проекта
GGgoBRO/Ibis-.ipv-editor; реализация своя, чистая. Нужны numpy + Pillow.

v0.2: extract как у JS-инструмента (layers/ + layers_trimmed/ + flattened.png +
manifest.json), пакетная замена replace-many (полные и кропнутые PNG с offset).

КОМАНДЫ:
  python ipv_canvas.py info         файл.ipv
  python ipv_canvas.py extract      файл.ipv папка
  python ipv_canvas.py replace      файл.ipv ID слой.png выход.ipv [--no-preview]
  python ipv_canvas.py replace-many файл.ipv папка_с_png выход.ipv [--no-preview]

РАСКЛАДКА extract:
  папка/layers/layer_ID.png                 полный холст, прозрачный фон
  папка/layers_trimmed/layer_ID_xX_yY.png   кроп по bbox (X,Y — от левого верха)
  папка/flattened.png                       все слои поверх белого (normal, opacity=1)
  папка/preview.png                         превью, сохранённое приложением
  папка/manifest.json                       id, порядок, bbox, selfcheck

replace-many берёт из папки файлы layer_ID.png (полный холст) и
layer_ID_xX_yY.png (кроп, вставляется в позицию X,Y). Можно скормить ей
папку layers/ или layers_trimmed/ прямо из extract после редактирования.

ГАРАНТИИ:
  * нетронутые кадры и gap'ы сохраняются побайтно;
  * каждый RPNG при чтении проверяется decode->encode->decode;
    не прошёл — слой не редактируется;
  * изменённый слой перекодируется: PNG-байты будут другими (это нормально),
    видимые пиксели — ровно твои (RGB под alpha=0 формат не хранит — это
    особенность RPNG, визуально без потерь).
УПРОЩЕНИЯ v0.2: превью = normal blend, opacity=1, все слои видимы,
порядок слоёв = порядок записей (1,5) в файле. История/таймлапс не трогается.
"""
import argparse
import io
import json
import os
import re
import struct
import sys

import numpy as np
from PIL import Image

T_CANVAS = 0x01000100
T_RASTER = 0x01000500
MANIFEST_FORMAT = 'IPV_CANVAS_MANIFEST_V1'
LAYER_PNG_RE = re.compile(r'^layer_(-?\d+)(?:_x(\d+)_y(\d+))?\.png$')


def make_frame(rtype, payload):
    n = len(payload)
    return struct.pack('>II', rtype, n) + payload + struct.pack('>i', -(n + 8))


def split_items(data):
    items, pos, n, gap_start = [], 0, len(data), 0
    while pos < n:
        ok = False
        if pos + 12 <= n:
            rtype, length = struct.unpack_from('>II', data, pos)
            end = pos + 8 + length
            if end + 4 <= n and struct.unpack_from('>i', data, end)[0] == -(length + 8):
                ok = True
        if ok:
            if gap_start < pos:
                items.append(('gap', data[gap_start:pos]))
            items.append(('frame', rtype, data[pos + 8:end]))
            pos = end + 4
            gap_start = pos
        else:
            pos += 1
    if gap_start < n:
        items.append(('gap', data[gap_start:n]))
    return items


# ======================= RPNG =======================
def rpng_decode(blob):
    if blob[:4] != b'RPNG':
        raise ValueError('not RPNG')
    hlen, z = struct.unpack_from('>II', blob, 4)
    if hlen != 45:
        raise ValueError(f'header len {hlen} != 45')
    flag = blob[12]
    cW, cH, bW, bH, bX, bY, pW, pH, pad, z2, idxBytes = struct.unpack_from('>11I', blob, 13)
    off = 12 + 45
    entries = [struct.unpack_from('>HH', blob, off + 4 * i) for i in range(idxBytes // 4)]
    png_len = struct.unpack_from('>I', blob, off + idxBytes)[0]
    png = blob[off + idxBytes + 4: off + idxBytes + 4 + png_len]
    im = np.asarray(Image.open(io.BytesIO(png)).convert('RGBA'))
    if im.shape != (pH, pW, 4):
        raise ValueError('PNG size mismatch')
    stream = im[::-1].reshape(-1, 4)          # строки PNG снизу вверх -> порядок скана
    parts, si = [], 0
    for gap, run in entries:
        si += gap
        if run:
            parts.append(np.arange(si, si + run, dtype=np.int64))
        si += run
    idx = np.concatenate(parts) if parts else np.empty(0, np.int64)
    if len(idx) + pad != pW * pH:
        raise ValueError('pixel count mismatch')
    canvas = np.zeros((cH, cW, 4), np.uint8)
    if len(idx):
        rows, cols = idx // bW, idx % bW
        ys, xs = cH - bY - 1 - rows, bX + cols
        canvas[ys, xs] = stream[:len(idx)]
    return dict(canvas=canvas, box=(int(bX), int(bY), int(bW), int(bH)),
                flag=flag, z2=z2, entries=entries)


def _entries_from_nz(nz, total):
    entries = []
    if len(nz) == 0:
        gap = total
        while gap > 65535:
            entries.append((65535, 0)); gap -= 65535
        entries.append((gap, 0))
        return entries
    d = np.diff(nz)
    starts = np.concatenate(([0], np.flatnonzero(d != 1) + 1))
    ends = np.concatenate((starts[1:], [len(nz)]))
    prev = 0
    for s, e in zip(starts, ends):
        gap = int(nz[s]) - prev
        run = int(e - s)
        while gap > 65535:
            entries.append((65535, 0)); gap -= 65535
        first = True
        while run > 65535:
            entries.append((gap if first else 0, 65535))
            first = False; run -= 65535
        entries.append((gap if first else 0, run))
        prev = int(nz[e - 1]) + 1
    gap = total - prev
    while gap > 65535:
        entries.append((65535, 0)); gap -= 65535
    entries.append((gap, 0))
    return entries


def rpng_encode(canvas, flag=0, z2=0):
    cH, cW = canvas.shape[:2]
    ys, xs = np.nonzero(canvas[:, :, 3])
    if len(xs) == 0:
        return b''
    x0, x1 = int(xs.min()), int(xs.max())
    y0, y1 = int(ys.min()), int(ys.max())
    bW, bH = x1 - x0 + 1, y1 - y0 + 1
    bX, bY = x0, cH - 1 - y1
    box = canvas[cH - bY - bH: cH - bY, bX: bX + bW]
    scan = box[::-1].reshape(-1, 4)           # нижняя строка первой
    nz = np.flatnonzero(scan[:, 3])
    count = len(nz)
    entries = _entries_from_nz(nz, bW * bH)
    pW = ((int(np.ceil(np.sqrt(count))) + 7) // 8) * 8
    pH = (count + pW - 1) // pW
    pad = pW * pH - count
    data = np.concatenate([scan[nz], np.zeros((pad, 4), np.uint8)])
    rows = np.ascontiguousarray(data.reshape(pH, pW, 4)[::-1])
    buf = io.BytesIO()
    Image.fromarray(rows, 'RGBA').save(buf, 'PNG')
    png = buf.getvalue()
    idx = b''.join(struct.pack('>HH', g, r) for g, r in entries)
    head = bytes([flag]) + struct.pack('>11I', cW, cH, bW, bH, bX, bY, pW, pH, pad, z2, len(idx))
    return b'RPNG' + struct.pack('>II', 45, 0) + head + idx + struct.pack('>I', len(png)) + png


def rpng_selfcheck(blob):
    d = rpng_decode(blob)
    re = rpng_encode(d['canvas'], d['flag'], d['z2'])
    if not re:
        return False, 'empty'
    d2 = rpng_decode(re)
    ok = (np.array_equal(d2['canvas'], d['canvas']) and d2['box'] == d['box']
          and d2['flag'] == d['flag'] and d2['z2'] == d['z2'])
    return ok, ('ok' if d2['entries'] == d['entries'] else 'ok (index differs: runs>65535?)')


# ======================= записи (1,5) =======================
def parse_raster(p):
    t, lid, kind, tag, ln = struct.unpack_from('>diiHI', p, 0)
    data = p[22:22 + ln]
    if len(data) != ln:
        raise ValueError('raster length mismatch')
    return dict(t=t, lid=lid, kind=kind, tag=tag, data=data)


def build_raster(r, data):
    return struct.pack('>diiHI', r['t'], r['lid'], r['kind'], r['tag'], len(data)) + data


def load_ipv(path):
    with open(path, 'rb') as f:
        data = f.read()
    items = split_items(data)
    cW = cH = None
    rasters = []
    for i, it in enumerate(items):
        if it[0] != 'frame':
            continue
        if it[1] == T_CANVAS and len(it[2]) >= 16:
            _, cW, cH, _ = struct.unpack_from('>dIIH', it[2], 0)
        if it[1] == T_RASTER:
            r = parse_raster(it[2])
            r['item'] = i
            if r['kind'] == 1 and r['data']:
                d = rpng_decode(r['data'])
                ok, note = rpng_selfcheck(r['data'])
                r.update(dec=d, ok=ok, note=note)
                if cW is None:
                    cW, cH = d['canvas'].shape[1], d['canvas'].shape[0]
            rasters.append(r)
    return data, items, rasters, (cW, cH)


def composite(layers, cW, cH):
    out = np.full((cH, cW, 3), 255.0)
    for rgba in layers:                      # снизу вверх, straight alpha, over white
        a = rgba[:, :, 3:4] / 255.0
        out = rgba[:, :, :3] * a + out * (1 - a)
    rgb = np.round(out).astype(np.uint8)
    return np.concatenate([rgb, np.full((cH, cW, 1), 255, np.uint8)], axis=2)


def save_png(arr, path):
    Image.fromarray(np.ascontiguousarray(arr), 'RGBA').save(path)


def bbox_top(d, cH):
    bX, bY, bW, bH = d['box']
    return int(bX), int(cH - bY - bH), int(bW), int(bH)   # x, y от верха, w, h


# ======================= команды =======================
def cmd_info(path):
    _, _, rasters, (cW, cH) = load_ipv(path)
    print(f'{path}: холст {cW}x{cH}')
    for r in rasters:
        if r['kind'] == 0:
            print(f'  preview: {len(r["data"])} байт PNG')
        elif not r['data']:
            print(f'  layer {r["lid"]}: пустой')
        else:
            print(f'  layer {r["lid"]}: box={r["dec"]["box"]}, selfcheck={r["note"]}')


def cmd_extract(path, outdir):
    _, _, rasters, (cW, cH) = load_ipv(path)
    os.makedirs(os.path.join(outdir, 'layers'), exist_ok=True)
    os.makedirs(os.path.join(outdir, 'layers_trimmed'), exist_ok=True)
    manifest = dict(format=MANIFEST_FORMAT, canvas=dict(width=int(cW), height=int(cH)), layers=[])
    layers, order = [], 0
    for r in rasters:
        if r['kind'] == 0:
            with open(os.path.join(outdir, 'preview.png'), 'wb') as f:
                f.write(r['data'])
            continue
        if r['kind'] != 1:
            continue
        arr = r['dec']['canvas'] if r['data'] else np.zeros((cH, cW, 4), np.uint8)
        layers.append(arr)
        save_png(arr, os.path.join(outdir, 'layers', f'layer_{r["lid"]}.png'))
        entry = dict(id=r['lid'], order=order, empty=not bool(r['data']))
        if r['data']:
            x, y, w, h = bbox_top(r['dec'], cH)
            save_png(arr[y:y + h, x:x + w],
                     os.path.join(outdir, 'layers_trimmed', f'layer_{r["lid"]}_x{x}_y{y}.png'))
            entry.update(bbox=dict(x=x, y=y, w=w, h=h), selfcheck=r['note'])
        manifest['layers'].append(entry)
        order += 1
    save_png(composite(layers, cW, cH), os.path.join(outdir, 'flattened.png'))
    with open(os.path.join(outdir, 'manifest.json'), 'w', encoding='utf-8') as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    print(f'[extract] слоёв: {order} -> {outdir}')


def apply_replacements(path, reps, out_path, preview=True):
    data, items, rasters, (cW, cH) = load_ipv(path)
    by_id = {r['lid']: r for r in rasters if r['kind'] == 1}
    for lid in reps:
        r = by_id.get(lid)
        if r is None:
            raise SystemExit(f'слой {lid} не найден')
        if r['data'] and not r['ok']:
            raise SystemExit(f'слой {lid} не прошёл selfcheck — не трогаю')
    blobs = {}
    for lid, arr in reps.items():
        r = by_id[lid]
        flag = r['dec']['flag'] if r['data'] else 0
        z2 = r['dec']['z2'] if r['data'] else 0
        blobs[lid] = rpng_encode(arr, flag, z2)
    layers = []
    for r in rasters:
        if r['kind'] != 1:
            continue
        arr = reps.get(r['lid'])
        if arr is None and r['data']:
            arr = r['dec']['canvas']
        if arr is not None:
            layers.append(arr)
    prev_png = None
    if preview:
        buf = io.BytesIO()
        Image.fromarray(composite(layers, cW, cH), 'RGBA').save(buf, 'PNG')
        prev_png = buf.getvalue()
    out = []
    for it in items:
        if it[0] == 'gap':
            out.append(it[1]); continue
        _, rtype, payload = it
        if rtype == T_RASTER:
            r = parse_raster(payload)
            if r['kind'] == 1 and r['lid'] in blobs:
                out.append(make_frame(rtype, build_raster(r, blobs[r['lid']]))); continue
            if r['kind'] == 0 and prev_png is not None:
                out.append(make_frame(rtype, build_raster(r, prev_png))); continue
        out.append(make_frame(rtype, payload))
    with open(out_path, 'wb') as f:
        f.write(b''.join(out))
    print(f'[replace] слоёв: {len(blobs)} -> {out_path}')


def cmd_replace(path, lid, png_path, out_path, preview=True):
    _, _, _, (cW, cH) = load_ipv(path)
    arr = np.asarray(Image.open(png_path).convert('RGBA'))
    if arr.shape[:2] != (cH, cW):
        raise SystemExit(f'PNG {arr.shape[1]}x{arr.shape[0]} != холст {cW}x{cH}')
    apply_replacements(path, {lid: arr}, out_path, preview)


def read_replacements(src, cW, cH):
    reps = {}
    for name in sorted(os.listdir(src)):
        m = LAYER_PNG_RE.match(name)
        if not m:
            continue
        lid = int(m.group(1))
        arr = np.asarray(Image.open(os.path.join(src, name)).convert('RGBA'))
        if m.group(2) is not None:
            x, y = int(m.group(2)), int(m.group(3))
            h, w = arr.shape[:2]
            if x + w > cW or y + h > cH:
                raise SystemExit(f'{name}: выходит за холст')
            full = np.zeros((cH, cW, 4), np.uint8)
            full[y:y + h, x:x + w] = arr
            arr = full
        elif arr.shape[:2] != (cH, cW):
            raise SystemExit(f'{name}: {arr.shape[1]}x{arr.shape[0]} != холст {cW}x{cH}')
        reps[lid] = arr
    if not reps:
        raise SystemExit('не нашёл layer_*.png в папке (см. раскладку extract)')
    return reps


def cmd_replace_many(path, src, out_path, preview=True):
    _, _, _, (cW, cH) = load_ipv(path)
    apply_replacements(path, read_replacements(src, cW, cH), out_path, preview)


def main(argv=None):
    ap = argparse.ArgumentParser(description='Редактор холста .ipv (RPNG), фулл-Python')
    sub = ap.add_subparsers(dest='cmd', required=True)
    a = sub.add_parser('info'); a.add_argument('ipv')
    a = sub.add_parser('extract'); a.add_argument('ipv'); a.add_argument('outdir')
    a = sub.add_parser('replace'); a.add_argument('ipv'); a.add_argument('lid', type=int)
    a.add_argument('png'); a.add_argument('out'); a.add_argument('--no-preview', action='store_true')
    a = sub.add_parser('replace-many'); a.add_argument('ipv'); a.add_argument('dir')
    a.add_argument('out'); a.add_argument('--no-preview', action='store_true')
    args = ap.parse_args(argv)
    if args.cmd == 'info':
        cmd_info(args.ipv)
    elif args.cmd == 'extract':
        cmd_extract(args.ipv, args.outdir)
    elif args.cmd == 'replace':
        cmd_replace(args.ipv, args.lid, args.png, args.out, preview=not args.no_preview)
    elif args.cmd == 'replace-many':
        cmd_replace_many(args.ipv, args.dir, args.out, preview=not args.no_preview)
    return 0


if __name__ == '__main__':
    sys.exit(main())
