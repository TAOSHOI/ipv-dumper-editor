#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ipv_dumper v1.1: точный дампер .ipv <-> JSON (без потерь).

КАК УСТРОЕНО (почему он «точный»):
  * файл режется на кадры (тип, длина, данные, контрольный трейлер); любые байты
    МЕЖДУ кадрами и в конце файла сохраняются как «gap», ничего не выбрасывается;
  * известные кадры (холст, штрих, заливка, превью, трекер) разбираются на поля;
  * каждый разобранный кадр СРАЗУ собирается обратно и сравнивается с оригиналом.
    Не совпало хоть на байт, кадр хранится «как есть» (kind=raw). Поэтому
    JSON -> .ipv всегда даёт побайтно тот же файл, даже если формат я понял не до конца;
  * большие и повторяющиеся байтовые блоки (хвосты кистей, PNG превью) лежат в таблице
    "blobs" один раз; JSON самодостаточный: донорский .ipv для сборки не нужен.

v1.1:
  * presets: кисти только из roundtrip-проверенных штрихов;
  * добавлен экспериментальный size_hint_brush12 (f32 на +12 brush-записи);
  * compose: thumbnail ищет превью (layerId=-1, kind=0), а не первый raster_cache.

КОМАНДЫ:
  python ipv_dumper.py dump    файл.ipv [файл.json]
  python ipv_dumper.py build   файл.json [файл.ipv]          точная сборка
  python ipv_dumper.py verify  файл_или_папка [--report r.txt]   dump -> JSON -> build -> сравнение
  python ipv_dumper.py inspect файл.ipv [отчёт.txt]            опись кадров (для разбора неизвестных)
  python ipv_dumper.py presets папка_или_файлы... --out lib.json   библиотека кистей из твоих файлов
  python ipv_dumper.py compose инструкции.json выход.ipv [--base база.json]

ИНСТРУКЦИИ (compose): JSON с ключами
  "base": "step0.json"          дамп любого .ipv (его холст, слои, превью берутся как основа)
  "brush_library": "lib.json"   (необязательно) библиотека кистей (команда presets или любой дамп)
  "canvas": {"width":1536, "height":2048, "project_id":"AI_0001"}   (необязательно)
  "thumbnail": "превью.png"     (необязательно) новое превью проекта
  "strokes": [ {"layer":1, "color":"#29253b", "preset":"имя",
                "points":[[x,y],[x,y,нажим],...], "t":12.5, "dur":0.4,
                "size":10.0 (экспериментально), "tool_mode":0 } ]
  Координаты в пикселях холста. Старые штрихи/заливки базы заменяются новыми.

В Colab из ячейки: from ipv_dumper import *   или   !python ipv_dumper.py verify /content/drive/...
"""
import os
import sys
import json
import struct
import base64
import hashlib
import argparse
import glob
import time
import copy
from collections import Counter

FORMAT = 'IPV_JSON_V1'
LIB_FORMAT = 'IPV_BRUSH_LIBRARY_V1'
INLINE_MAX = 64          # байтовые поля короче этого лежат прямо в JSON (hex)

T_CANVAS, T_SESSION, T_TRACKER, T_RASTER, T_FOOTER = 0x01000100, 0x01000200, 0x01000300, 0x01000500, 0x01000600
T_VIEWPORT, T_TOOL, T_STROKE, T_POINT = 0x02000100, 0x02000200, 0x02000300, 0x02000301
T_FILL, T_EYEDROP, T_BRUSHP, T_LASSO = 0x02000400, 0x02000500, 0x02000600, 0x02000800
T_FX, T_RULER, T_LIQUIFY, T_LIQ_PT = 0x02000900, 0x02000A00, 0x02000C00, 0x02000C03
T_LAYERS, T_LAYER_DESC, T_LAYER_CMD, T_LAYER_TX = 0x03000400, 0x03000402, 0x03000500, 0x03000600

NAMES = {
    T_CANVAS: 'canvas_init', T_SESSION: 'session_env', T_TRACKER: 'playback_tracker', T_RASTER: 'raster_cache',
    T_FOOTER: 'metadata_footer', T_VIEWPORT: 'viewport_state', T_TOOL: 'tool_select', T_STROKE: 'stroke',
    T_POINT: 'stroke_point', T_FILL: 'bucket_fill', T_EYEDROP: 'eyedropper', T_BRUSHP: 'brush_param',
    T_LASSO: 'selection_lasso', T_FX: 'fx_filter', T_RULER: 'ruler_config', T_LIQUIFY: 'liquify_stroke',
    T_LIQ_PT: 'liquify_point', T_LAYERS: 'layer_stack', T_LAYER_DESC: 'layer_descriptor',
    T_LAYER_CMD: 'layer_property_cmd', T_LAYER_TX: 'layer_transaction',
}
ACTION_TYPES = (T_STROKE, T_FILL, T_LASSO, T_LIQUIFY, T_FX)   # что заменяет compose
BYTE_KEYS = ('tail', 'header', 'png', 'rest', 'data')
PNG_MAGIC = b'\x89PNG\r\n\x1a\n'


# ======================= низкий уровень =======================
def make_frame(rtype, payload):
    n = len(payload)
    return struct.pack('>II', rtype, n) + payload + struct.pack('>i', -(n + 8))


def split_items(data):
    """Кадры и «щели» между ними. Конкатенация всего даёт исходные байты."""
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


class Blobs:
    """Таблица байтовых блоков: одинаковые блоки хранятся один раз."""

    def __init__(self, table=None):
        self.table = dict(table) if table else {}
        self._cache = {}

    def put(self, b):
        b = bytes(b)
        if len(b) <= INLINE_MAX:
            return 'hex:' + b.hex()
        enc = base64.b64encode(b).decode('ascii')
        base = hashlib.sha1(b).hexdigest()[:16]
        key, n = base, 0
        while key in self.table and self.table[key] != enc:
            n += 1
            key = f'{base}_{n}'
        self.table[key] = enc
        return 'blob:' + key

    def get(self, ref):
        if ref.startswith('hex:'):
            return bytes.fromhex(ref[4:])
        if ref.startswith('blob:'):
            k = ref[5:]
            if k not in self._cache:
                self._cache[k] = base64.b64decode(self.table[k])
            return self._cache[k]
        raise ValueError(f'неизвестная ссылка на байты: {ref[:20]!r}')


# ======================= точки =======================
def read_points(p, pos, count):
    """count подкадров точек подряд. Возвращает (точки, sl, позиция_после) или None."""
    pts, sl0 = [], None
    for _ in range(count):
        if pos + 12 > len(p):
            return None
        st, sl = struct.unpack_from('>II', p, pos)
        if st != T_POINT or sl < 16 or (sl - 16) % 4 or pos + 12 + sl > len(p):
            return None
        if sl0 is None:
            sl0 = sl
        elif sl != sl0:
            return None
        if struct.unpack_from('>i', p, pos + 8 + sl)[0] != -(sl + 8):
            return None
        pts.append(list(struct.unpack_from('>dff' + 'f' * ((sl - 16) // 4), p, pos + 8)))
        pos += 12 + sl
    return pts, (sl0 if sl0 is not None else 16), pos


def write_points(pts, sl):
    n_extra = (sl - 16) // 4
    fmt = '>dff' + 'f' * n_extra
    out = []
    for pt in pts:
        if len(pt) != 3 + n_extra:
            raise ValueError(f'в точке {len(pt)} чисел, а для sl={sl} нужно {3 + n_extra}')
        out.append(struct.pack('>II', T_POINT, sl) + struct.pack(fmt, *pt) + struct.pack('>i', -(sl + 8)))
    return b''.join(out)


# ======================= разбор / сборка известных кадров =======================
def parse_canvas(p):
    if len(p) < 18:
        return None
    ts, w, h, n = struct.unpack_from('>dIIH', p, 0)
    if 18 + n > len(p):
        return None
    return dict(ts=ts, width=w, height=h, project_id=p[18:18 + n].decode('latin1'), tail=p[18 + n:])


def encode_canvas(f):
    pid = f['project_id'].encode('latin1')
    return struct.pack('>dIIH', f['ts'], f['width'], f['height'], len(pid)) + pid + f['tail']


def parse_stroke(p):
    if len(p) < 40:
        return None
    ts_s, ts_e, mode, shape, cat, color, layer, cnt = struct.unpack_from('>dd6I', p, 0)
    r = read_points(p, 40, cnt)
    if r is None:
        return None
    pts, sl, pos = r
    return dict(ts_start=ts_s, ts_end=ts_e, tool_mode=mode, brush_shape=shape, tool_cat=cat,
                color=f'{color:08x}', layer=layer, sl=sl, points=pts, tail=p[pos:])


def encode_stroke(f):
    head = struct.pack('>dd6I', f['ts_start'], f['ts_end'], f['tool_mode'], f['brush_shape'],
                       f['tool_cat'], int(f['color'], 16), f['layer'], len(f['points']))
    return head + write_points(f['points'], f['sl']) + f['tail']


def parse_fill(p):
    if len(p) < 28:
        return None
    ts_s, ts_e, n1 = struct.unpack_from('>ddI', p, 0)
    r1 = read_points(p, 20, n1)
    if r1 is None:
        return None
    seeds, sl1, pos = r1
    if pos + 8 > len(p):
        return None
    color, n2 = struct.unpack_from('>II', p, pos)
    r2 = read_points(p, pos + 8, n2)
    if r2 is None:
        return None
    seeds2, sl2, pos2 = r2
    return dict(ts_start=ts_s, ts_end=ts_e, sl=sl1, seeds=seeds, color=f'{color:08x}',
                sl2=sl2, seeds2=seeds2, rest=p[pos2:])


def encode_fill(f):
    return (struct.pack('>ddI', f['ts_start'], f['ts_end'], len(f['seeds'])) + write_points(f['seeds'], f['sl']) +
            struct.pack('>II', int(f['color'], 16), len(f['seeds2'])) + write_points(f['seeds2'], f['sl2']) + f['rest'])


def parse_raster(p):
    i = p.find(PNG_MAGIC)
    if i < 0:
        return None
    return dict(header=p[:i], png=p[i:])


def encode_raster(f):
    return f['header'] + f['png']


def parse_tracker(p):
    if len(p) < 16:
        return None
    ts_s, ts_e = struct.unpack_from('>dd', p, 0)
    return dict(ts_start=ts_s, ts_end=ts_e, rest=p[16:])


def encode_tracker(f):
    return struct.pack('>dd', f['ts_start'], f['ts_end']) + f['rest']


CODECS = {  # тип кадра -> (имя вида, разбор, сборка)
    T_CANVAS: ('canvas_init', parse_canvas, encode_canvas),
    T_STROKE: ('stroke', parse_stroke, encode_stroke),
    T_FILL: ('fill', parse_fill, encode_fill),
    T_RASTER: ('raster_cache', parse_raster, encode_raster),
    T_TRACKER: ('playback_tracker', parse_tracker, encode_tracker),
}
KIND_ENC = {k: (t, e) for t, (k, _, e) in CODECS.items()}


def json_roundtrip(fields):
    """Поля так, как они вернутся из JSON (байтовые поля не трогаем)."""
    plain = {k: v for k, v in fields.items() if not isinstance(v, bytes)}
    out = dict(fields)
    out.update(json.loads(json.dumps(plain, ensure_ascii=False)))
    return out


def classify(rtype, payload):
    """(kind, поля). Если разбор не собирается обратно в те же байты, кадр остаётся raw."""
    c = CODECS.get(rtype)
    if c:
        kind, parse, enc = c
        try:
            fields = parse(payload)
            # проверяем сборку уже ПОСЛЕ прохода через JSON (NaN с особыми битами и т.п. там теряются)
            if fields is not None and enc(json_roundtrip(fields)) == payload:
                return kind, fields, False
        except Exception:
            pass
        return 'raw', dict(type=f'0x{rtype:08x}', name=NAMES.get(rtype, 'unknown'), data=payload), True
    return 'raw', dict(type=f'0x{rtype:08x}', name=NAMES.get(rtype, 'unknown'), data=payload), False


# ======================= кисти (пресеты) =======================
def preset_name(f):
    h = hashlib.sha1(f['tail']).hexdigest()[:6]
    return f"cat{f['tool_cat']:04x}_s{f['brush_shape']}_m{f['tool_mode']}_sl{f['sl']}_{h}"


def note_preset(presets, f):
    name = preset_name(f)
    e = presets.get(name)
    if e is None:
        e = presets[name] = dict(tool_mode=f['tool_mode'], brush_shape=f['brush_shape'], tool_cat=f['tool_cat'],
                                 sl=f['sl'], tail=f['tail'], count=0,
                                 extras_example=(f['points'][0][3:] if f['points'] else []))
    e['count'] += 1


def brush_size_hint(tail):
    """ЭКСПЕРИМЕНТАЛЬНО: f32 на +12 вложенной brush-записи (1,2,sub2)."""
    for it in split_items(tail):
        if it[0] == 'frame' and it[1] == 0x01000202 and len(it[2]) >= 16:
            return struct.unpack('>f', it[2][12:16])[0]
    return None


def finish_presets(presets, blobs, top=None):
    items = sorted(presets.items(), key=lambda kv: -kv[1]['count'])
    if top:
        items = items[:top]
    out = {}
    for name, e in items:
        tail = e['tail']
        hint = None
        if len(tail) >= 8:
            hint = struct.unpack('>f', tail[-4:])[0]
        out[name] = dict(tool_mode=e['tool_mode'], brush_shape=e['brush_shape'], tool_cat=e['tool_cat'],
                         sl=e['sl'], count=e['count'], extras_example=e['extras_example'],
                         size_hint_last_f32=hint, size_hint_brush12=brush_size_hint(tail),
                         tail=blobs.put(tail))
    return out


# ======================= дамп =======================
def dump_bytes(data, name=''):
    blobs, frames, fallbacks = Blobs(), [], Counter()
    kinds, presets = Counter(), {}
    ts_min, ts_max, canvas = None, None, None

    for it in split_items(data):
        if it[0] == 'gap':
            frames.append(dict(kind='gap', data=blobs.put(it[1])))
            kinds['gap'] += 1
            continue
        _, rtype, payload = it
        kind, fields, failed = classify(rtype, payload)
        if failed:
            fallbacks[f'0x{rtype:08x}'] += 1
        kinds[kind if kind != 'raw' else NAMES.get(rtype, 'unknown') + ('(raw!)' if failed else '')] += 1
        if kind == 'stroke':
            note_preset(presets, fields)
            ts_min = fields['ts_start'] if ts_min is None else min(ts_min, fields['ts_start'])
            ts_max = fields['ts_end'] if ts_max is None else max(ts_max, fields['ts_end'])
        if kind == 'canvas_init' and canvas is None:
            canvas = dict(width=fields['width'], height=fields['height'], project_id=fields['project_id'])
        for k in BYTE_KEYS:
            if k in fields:
                fields[k] = blobs.put(fields[k])
        frames.append(dict(kind=kind, **fields))

    doc = dict(
        format=FORMAT,
        source=dict(file=name, size=len(data), sha256=hashlib.sha256(data).hexdigest()),
        summary=dict(frames=len(frames), kinds=dict(kinds), stroke_time=[ts_min, ts_max],
                     parse_fallbacks_to_raw=dict(fallbacks)),
        canvas=canvas,
        presets=finish_presets(presets, blobs),
        frames=frames,
        blobs=blobs.table,
    )
    return doc


# ======================= сборка =======================
def deref(fr, blobs):
    f = dict(fr)
    for k in BYTE_KEYS:
        if k in f:
            f[k] = blobs.get(f[k])
    return f


def build_bytes(doc):
    if doc.get('format') != FORMAT:
        raise ValueError(f'ожидался формат {FORMAT}, а в файле: {doc.get("format")}')
    blobs = Blobs(doc.get('blobs', {}))
    out = []
    for i, fr in enumerate(doc['frames']):
        kind = fr['kind']
        try:
            if kind == 'gap':
                out.append(blobs.get(fr['data']))
            elif kind == 'raw':
                out.append(make_frame(int(fr['type'], 16), blobs.get(fr['data'])))
            elif kind in KIND_ENC:
                rtype, enc = KIND_ENC[kind]
                out.append(make_frame(rtype, enc(deref(fr, blobs))))
            else:
                raise ValueError(f'неизвестный вид кадра: {kind}')
        except Exception as e:
            raise ValueError(f'кадр #{i} ({kind}): {e}') from e
    return b''.join(out)


def verify_bytes(data):
    """dump -> текст JSON -> разбор -> build -> сравнение с оригиналом."""
    t0 = time.time()
    doc = dump_bytes(data)
    text = json.dumps(doc, ensure_ascii=False, separators=(',', ':'))
    rebuilt = build_bytes(json.loads(text))
    return dict(ok=rebuilt == data, size=len(data), json_mb=len(text) / 1e6,
                strokes=doc['summary']['kinds'].get('stroke', 0), fills=doc['summary']['kinds'].get('fill', 0),
                fallbacks=doc['summary']['parse_fallbacks_to_raw'], gaps=doc['summary']['kinds'].get('gap', 0),
                seconds=time.time() - t0)


# ======================= опись кадров =======================
def inspect_bytes(data, name=''):
    items = split_items(data)
    lines = [f'Файл {name}: {len(data)} байт, элементов: {len(items)}', '']
    census = {}
    shown_strokes = 0
    for idx, it in enumerate(items):
        if it[0] == 'gap':
            lines.append(f'#{idx} GAP {len(it[1])} байт: {it[1][:48].hex()}')
            continue
        _, rtype, payload = it
        nm = NAMES.get(rtype, 'unknown')
        c = census.setdefault((rtype, nm), [0, 0, 10 ** 12, 0])
        c[0] += 1
        c[1] += len(payload)
        c[2] = min(c[2], len(payload))
        c[3] = max(c[3], len(payload))
        if rtype == T_STROKE:
            shown_strokes += 1
            if shown_strokes > 2:
                continue
        elif c[0] > 3 and nm not in ('canvas_init', 'session_env', 'layer_stack', 'metadata_footer'):
            continue
        head = payload[:96].hex()
        tail = payload[-32:].hex() if len(payload) > 96 else ''
        lines.append(f'#{idx} 0x{rtype:08x} {nm} len={len(payload)}')
        lines.append(f'    начало: {head}')
        if tail:
            lines.append(f'    конец:  {tail}')
    lines += ['', 'ПЕРЕПИСЬ ТИПОВ (тип, имя, штук, всего байт, мин, макс):']
    for (rtype, nm), (n, tot, mn, mx) in sorted(census.items()):
        lines.append(f'  0x{rtype:08x} {nm:<18} {n:>7} {tot:>10} {mn:>8} {mx:>8}')
    return '\n'.join(lines)


# ======================= библиотека кистей =======================
def collect_presets(paths, top=300):
    presets, blobs = {}, Blobs()
    for p in paths:
        with open(p, 'rb') as f:
            data = f.read()
        for it in split_items(data):
            if it[0] != 'frame' or it[1] != T_STROKE:
                continue
            f_ = parse_stroke(it[2])
            if f_ is not None:
                # v1.1: в библиотеку только roundtrip-проверенные штрихи
                try:
                    if encode_stroke(json_roundtrip(f_)) != it[2]:
                        continue
                except Exception:
                    continue
                note_preset(presets, f_)
    return dict(format=LIB_FORMAT, presets=finish_presets(presets, blobs, top), blobs=blobs.table)


# ======================= compose: сборка по инструкциям =======================
def parse_color(c):
    if isinstance(c, str):
        s = c.lstrip('#').lower()
        if len(s) == 6:
            s += 'ff'
        if len(s) != 8:
            raise ValueError(f'цвет должен быть #RRGGBB или #RRGGBBAA, а тут: {c!r}')
        int(s, 16)
        return s
    r, g, b = [int(round(v * 255)) if isinstance(v, float) and v <= 1.0 else int(v) for v in c[:3]]
    a = int(c[3]) if len(c) > 3 else 255
    return f'{r:02x}{g:02x}{b:02x}{a:02x}'


def arc_fractions(xy):
    d = [0.0]
    for i in range(1, len(xy)):
        d.append(d[-1] + ((xy[i][0] - xy[i - 1][0]) ** 2 + (xy[i][1] - xy[i - 1][1]) ** 2) ** 0.5)
    total = d[-1]
    return [x / total if total > 1e-9 else i / max(len(d) - 1, 1) for i, x in enumerate(d)], total


def load_doc(path):
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def compose(instr, base_dir='.', base_override=None):
    base_path = base_override or instr.get('base')
    if not base_path:
        raise ValueError('в инструкциях нет "base" (дамп любого .ipv, например step0.json)')
    if not os.path.isabs(base_path):
        base_path = os.path.join(base_dir, base_path)
    doc = load_doc(base_path)
    if doc.get('format') != FORMAT:
        raise ValueError('"base" должен быть дампом этого дампера (команда dump)')
    blobs = Blobs(doc.get('blobs', {}))
    presets = dict(doc.get('presets', {}))

    lib = instr.get('brush_library')
    if lib:
        lib_path = lib if os.path.isabs(lib) else os.path.join(base_dir, lib)
        ldoc = load_doc(lib_path)
        lib_blobs = Blobs(ldoc.get('blobs', {}))
        for name, pr in ldoc.get('presets', {}).items():
            pr = dict(pr)
            pr['tail'] = blobs.put(lib_blobs.get(pr['tail']))   # переносим хвост в нашу таблицу
            presets.setdefault(name, pr)
    if not presets:
        raise ValueError('нет кистей: в базе нет штрихов, а "brush_library" не указана. '
                         'Сделай библиотеку: python ipv_dumper.py presets папка_с_ipv --out lib.json')

    frames = doc['frames']
    canvas = next((f for f in frames if f['kind'] == 'canvas_init'), None)
    cv = instr.get('canvas') or {}
    if canvas is not None:
        for k in ('width', 'height', 'project_id'):
            if k in cv:
                canvas[k] = cv[k]
    ts_base = canvas['ts'] if canvas else 1.7e9

    th = instr.get('thumbnail')
    if th:
        th_path = th if os.path.isabs(th) else os.path.join(base_dir, th)
        with open(th_path, 'rb') as f:
            png = f.read()
        # v1.1: превью — это raster с layerId=-1 и kind=0; первый raster может быть слоем (RPNG)
        ras = None
        for f in frames:
            if f['kind'] == 'raster_cache':
                hdr = blobs.get(f['header'])
                if len(hdr) >= 16 and struct.unpack_from('>ii', hdr, 8) == (-1, 0):
                    ras = f
                    break
        if ras is None:
            ras = next((f for f in frames if f['kind'] == 'raster_cache'), None)
        if ras is None:
            raise ValueError('в базе нет кадра превью (raster_cache), "thumbnail" применить нельзя')
        ras['png'] = blobs.put(png)

    default_preset = max(presets.items(), key=lambda kv: kv[1].get('count', 0))[0]
    new, cursor = [], ts_base + 1.0
    for i, s in enumerate(instr.get('strokes', [])):
        name = s.get('preset', default_preset)
        if name not in presets:
            raise ValueError(f'штрих #{i}: нет кисти "{name}". Есть, например: {list(presets)[:3]}')
        pr = presets[name]
        pts = s['points']
        if len(pts) == 0:
            raise ValueError(f'штрих #{i}: нет точек')
        xy = [(float(p[0]), float(p[1])) for p in pts]
        if len(xy) == 1:
            xy, pts = xy * 2, pts * 2
        frac, length = arc_fractions(xy)
        dur = float(s['dur']) if 'dur' in s else max(0.04, length / 900.0)
        ts_s = ts_base + 1.0 + float(s['t']) if 't' in s else cursor
        ts_e = ts_s + dur
        cursor = ts_e + 0.015
        sl = pr['sl']
        n_extra = (sl - 16) // 4
        ex_default = list(pr.get('extras_example', []))[:n_extra]
        ex_default += [0.0] * (n_extra - len(ex_default))
        rows = []
        for j, p in enumerate(pts):
            ex = list(ex_default)
            if n_extra >= 1:
                ex[0] = float(p[2]) if len(p) > 2 else 0.8
            rows.append([ts_s + frac[j] * dur, xy[j][0], xy[j][1]] + ex)
        tail = blobs.get(pr['tail'])
        if 'size' in s and len(tail) >= 8:        # ЭКСПЕРИМЕНТАЛЬНО: размер кисти в конце хвоста
            tail = tail[:-4] + struct.pack('>f', float(s['size']))
        new.append(dict(kind='stroke', ts_start=ts_s, ts_end=ts_e,
                        tool_mode=int(s.get('tool_mode', pr['tool_mode'])), brush_shape=pr['brush_shape'],
                        tool_cat=pr['tool_cat'], color=parse_color(s.get('color', '#000000')),
                        layer=int(s.get('layer', 1)), sl=sl, points=rows, tail=blobs.put(tail)))

    keep, first_removed = [], None
    for i, fr in enumerate(frames):
        is_action = fr['kind'] in ('stroke', 'fill') or (
            fr['kind'] == 'raw' and int(fr['type'], 16) in ACTION_TYPES)
        if is_action:
            if first_removed is None:
                first_removed = len(keep)
        else:
            keep.append(fr)
    if first_removed is None:
        first_removed = next((i for i, fr in enumerate(keep) if fr['kind'] == 'playback_tracker' or
                              (fr['kind'] == 'raw' and int(fr['type'], 16) == T_FOOTER)), len(keep))
    doc['frames'] = keep[:first_removed] + new + keep[first_removed:]

    if instr.get('tracker_end') and new:
        for fr in doc['frames']:
            if fr['kind'] == 'playback_tracker':
                fr['ts_end'] = new[-1]['ts_end'] + 1.0
    doc['blobs'] = blobs.table
    doc['source'] = dict(file='composed', size=None, sha256=None)
    return doc


# ======================= ввод-вывод =======================
def dump_file(path, out_json=None, pretty=False):
    with open(path, 'rb') as f:
        data = f.read()
    doc = dump_bytes(data, os.path.basename(path))
    out_json = out_json or os.path.splitext(path)[0] + '.json'
    with open(out_json, 'w', encoding='utf-8') as f:
        json.dump(doc, f, ensure_ascii=False, indent=1 if pretty else None, separators=(None if pretty else (',', ':')))
    fb = doc['summary']['parse_fallbacks_to_raw']
    print(f'[dump] {path} -> {out_json}')
    print(f'  кадров: {doc["summary"]["frames"]}, по видам: {doc["summary"]["kinds"]}')
    print(f'  кадров, разобранных не полностью (хранятся как есть): {fb if fb else "нет"}')
    return doc


def build_file(path, out_ipv=None):
    doc = load_doc(path)
    data = build_bytes(doc)
    out_ipv = out_ipv or os.path.splitext(path)[0] + '_rebuilt.ipv'
    with open(out_ipv, 'wb') as f:
        f.write(data)
    sha = doc.get('source', {}).get('sha256')
    print(f'[build] {path} -> {out_ipv} ({len(data)} байт)')
    if sha:
        same = hashlib.sha256(data).hexdigest() == sha
        print('  совпадает с исходным .ipv побайтно' if same else '  отличается от исходного .ipv (JSON правили?)')
    return data


def verify_paths(paths, report=None):
    files = []
    for p in paths:
        files += sorted(glob.glob(os.path.join(p, '*.ipv'))) if os.path.isdir(p) else [p]
    lines, bad = [], 0
    for i, p in enumerate(files, 1):
        with open(p, 'rb') as f:
            data = f.read()
        r = verify_bytes(data)
        bad += 0 if r['ok'] else 1
        line = (f'[{i}/{len(files)}] {os.path.basename(p)}: {"OК" if r["ok"] else "НЕ СОВПАЛО"} | '
                f'{r["size"] / 1e6:.1f} МБ, штрихов {r["strokes"]}, заливок {r["fills"]}, щелей {r["gaps"]}, '
                f'json {r["json_mb"]:.0f} МБ, {r["seconds"]:.0f} с, в raw: {r["fallbacks"] or "-"}')
        lines.append(line)
        print(line)
    summary = f'ИТОГО: файлов {len(files)}, побайтно совпало {len(files) - bad}, не совпало {bad}'
    lines.append(summary)
    print(summary)
    if report:
        with open(report, 'w', encoding='utf-8') as f:
            f.write('\n'.join(lines))
    return bad == 0


def main(argv=None):
    ap = argparse.ArgumentParser(description='Точный дампер ibisPaint .ipv <-> JSON')
    sub = ap.add_subparsers(dest='cmd', required=True)
    a = sub.add_parser('dump'); a.add_argument('ipv'); a.add_argument('json', nargs='?'); a.add_argument('--pretty', action='store_true')
    a = sub.add_parser('build'); a.add_argument('json'); a.add_argument('ipv', nargs='?')
    a = sub.add_parser('verify'); a.add_argument('paths', nargs='+'); a.add_argument('--report')
    a = sub.add_parser('inspect'); a.add_argument('ipv'); a.add_argument('out', nargs='?')
    a = sub.add_parser('presets'); a.add_argument('paths', nargs='+'); a.add_argument('--out', required=True); a.add_argument('--top', type=int, default=300)
    a = sub.add_parser('compose'); a.add_argument('instructions'); a.add_argument('out'); a.add_argument('--base')
    args = ap.parse_args(argv)

    if args.cmd == 'dump':
        dump_file(args.ipv, args.json, args.pretty)
    elif args.cmd == 'build':
        build_file(args.json, args.ipv)
    elif args.cmd == 'verify':
        return 0 if verify_paths(args.paths, args.report) else 1
    elif args.cmd == 'inspect':
        with open(args.ipv, 'rb') as f:
            text = inspect_bytes(f.read(), os.path.basename(args.ipv))
        if args.out:
            with open(args.out, 'w', encoding='utf-8') as f:
                f.write(text)
            print('Опись сохранена:', args.out)
        else:
            print(text)
    elif args.cmd == 'presets':
        files = []
        for p in args.paths:
            files += sorted(glob.glob(os.path.join(p, '*.ipv'))) if os.path.isdir(p) else [p]
        lib = collect_presets(files, args.top)
        with open(args.out, 'w', encoding='utf-8') as f:
            json.dump(lib, f, ensure_ascii=False, separators=(',', ':'))
        print(f'Кистей в библиотеке: {len(lib["presets"])} (из {len(files)} файлов). Файл: {args.out}')
        for name, pr in list(lib['presets'].items())[:8]:
            print(f'  {name}: штрихов {pr["count"]}, размер(гипотеза) {pr["size_hint_last_f32"]}')
    elif args.cmd == 'compose':
        with open(args.instructions, 'r', encoding='utf-8') as f:
            instr = json.load(f)
        doc = compose(instr, os.path.dirname(os.path.abspath(args.instructions)), args.base)
        data = build_bytes(doc)
        with open(args.out, 'wb') as f:
            f.write(data)
        print(f'[compose] {args.out}: {len(data)} байт, штрихов {sum(1 for fr in doc["frames"] if fr["kind"] == "stroke")}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
