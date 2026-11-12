# ipv-dumper

Lossless **.ipv (ibisPaint X) ⇄ JSON** dumper with a **byte-exact round-trip guarantee**:
`dump → JSON → build` reproduces the original file bit-for-bit, even for records
the parser does not fully understand.

Python 3.8+, standard library only. No installation.

## Why it is "exact"

- The file is split into records (`type u32, len u32, payload, -(len+8) i32` trailer).
  Any bytes **between** records and at the end of the file are preserved as `gap`s.
- Known records (canvas, stroke, fill, raster, tracker) are parsed into fields, then
  **immediately re-encoded and compared to the original after a JSON round-trip**.
  One byte off → the record is stored as opaque `raw`. Nothing is ever guessed silently.
- Large/repeated byte blocks (brush tails, PNGs) are deduplicated into a `blobs` table.
  The JSON is self-contained: no donor .ipv needed to rebuild.

## Usage

```
python ipv_dumper.py dump    file.ipv [file.json]
python ipv_dumper.py build   file.json [file.ipv]        # exact rebuild
python ipv_dumper.py verify  file_or_dir [--report r.txt]  # dump->build->compare
python ipv_dumper.py inspect file.ipv [report.txt]       # record census
python ipv_dumper.py presets dir_or_files --out lib.json # brush preset library
python ipv_dumper.py compose instructions.json out.ipv [--base base.json]
~~~

### compose: synthesize strokes

```json
{
  "base": "dump_of_any.json",
  "brush_library": "lib.json",
  "canvas": {"width": 1536, "height": 2048, "project_id": "AI_0001"},
  "thumbnail": "preview.png",
  "strokes": [
    {"layer": 1, "color": "#29253b", "preset": "cat7000_s1_m0_sl20_ab12cd",
     "points": [[10, 10], [200, 150, 0.5], [300, 80]], "dur": 0.5}
  ]
}
```

Coordinates are canvas pixels; optional third value per point is pressure (0..1).

## What is decoded

| Record | Status |
|---|---|
| canvas init, stroke envelope + points, bucket fill, raster (PNG/RPNG split), tracker | parsed, round-trip verified |
| everything else (layers, footer, liquify, lasso, fx, …) | preserved as `raw`, byte-exact |

Format notes for the event stream (strokes / fills / pressure): [docs/IPV_FORMAT-strokes.md](docs/IPV_FORMAT-strokes.md).

## Experimental / limitations

- Brush **size** hints (`size_hint_brush12`, `size_hint_last_f32`) are hypotheses — see docs.
- Extra per-point float treated as **pressure** is a working hypothesis (🟡).
- `compose` rewrites the stroke **history** (what the app replays for the timelapse).
  The displayed canvas is restored from stored layer bitmaps (RPNG) — rasterizing
  synthesized strokes into layers is on the roadmap.
- NaN/Infinity appear as non-standard JSON tokens; fine for this tool's own loader.

## Self-test

~~~
python tests/test_roundtrip.py
```

Builds a synthetic .ipv (gaps, unknown records, NaN-bit trap, RPNG-embedded PNG)
and checks round-trip, raw fallback, preset filtering and compose. Stdlib only.

## Credits

Container framing, RPNG layer bitmaps and footer were independently reverse-engineered
by [GGgoBRO/Ibis-.ipv-editor](https://github.com/GGgoBRO/Ibis-.ipv-editor) — our results
match byte-for-byte. This project focuses on the complementary half: the event stream
(strokes, fills, points, brush presets) and lossless synthesis.

Unofficial tool, not affiliated with ibis Inc. Use it with your own files.

## License
   Apache-2.0
