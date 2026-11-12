
Title: Stroke (2,3) / fill (2,4) internals decoded independently — complementary findings

Hi! I independently reverse-engineered the event-stream half of .ipv in Python
while you were doing the state half — our container framing, canvas record and
stroke header match byte-for-byte, which is a nice mutual confirmation.

Findings that fill some of your 🟡/❓ gaps (tested on ibisPaint X <APP_VERSION>, Android):

- Stroke (2,3) header fields named: tool_mode / brush_shape / tool_category /
  color (RRGGBBAA) / target layer id / point count.
- Point subframes (2,3,1): payload length 16 AND 20+ observed. With sl>=20 the
  first extra f32 behaves like pressure (0..1) — this contradicts the
  "No pressure is stored" note; possibly version/stylus dependent.
- (2,4) bucket fill: seed points, RGBA color, tolerance f32, and a tag that
  matched a layer id in most samples.
- (2,8) lasso, (2,12)+(2,12,3) liquify observed (preserved, not decoded).
- Brush size: your +12 f32 hypothesis looks right; the last f32 of the tail is
  NOT size (it lands inside the 30-byte (2,3,3) record).

Tool: <[Issues Link](https://github.com/TAOSHOI/ipv-dumper-editor/issues)> — lossless dump/build with a byte-exact round-trip
guarantee (unknown records kept verbatim), brush preset library, stroke synthesis.
Notes: docs/IPV_FORMAT-strokes.md, in your ✅/🟡/❓ style.

Happy to PR an IPV_FORMAT.md extension and generate the minimal one-change
sample files from your §11 list. Would you be interested?
