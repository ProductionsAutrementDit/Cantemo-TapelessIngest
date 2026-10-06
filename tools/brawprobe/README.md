# brawprobe

One Blackmagic RAW clip in, one JSON object out. The TapelessIngest
`braw` provider (`providers/braw.py`) runs it once per clip at SCAN time
and stores everything it prints; nothing runs it at import.

```
brawprobe [--sdk-dir DIR] [--] <clip.braw>
```

`--` ends the options, so a path starting with `-` is still a path; the
provider always passes it.

`DIR` is the folder holding the Blackmagic RAW library; it defaults to
`SDK_DIR/Libraries` as given at build time.

## Output

stdout carries exactly one JSON object:

| field | meaning |
|---|---|
| `brawprobe_version` | `"1"` |
| `clip`, `path` | the clip's file name, and the path as given |
| `sdk_dir` | the library directory loaded |
| `camera` | `GetCameraType` |
| `width`, `height`, `frame_count` | the clip's geometry and length |
| `fps_num`, `fps_den`, `fps_reported` | frame rate, snapped as brawdump does, and the SDK's float |
| `sensor_rate_num`, `sensor_rate_den`, `sensor_rate_reported` | sensor rate of frame 0, else of the first readable of frames 1–7 |
| `offspeed` | snapped sensor rate ≠ snapped frame rate |
| `timecode` | frame 0, `hh:mm:ss:ff` (dot separators rewritten) |
| `gamma_recorded`, `gamut` | the clip's processing attributes |
| `audio_channels`, `audio_sample_rate`, `audio_bits`, `audio_samples` | all four or nothing: all 0 when the clip has no audio, when any getter fails, or when any of them is 0 |
| `frame0_readable` | whether frame 0 could be read (its metadata is `{}` otherwise) |
| `excluded` | metadata keys left out, as `<scope>.<key>` |
| `metadata.clip`, `metadata.frame0` | EVERY key of the clip's and frame 0's metadata iterators, typed: numbers as numbers, strings as strings, SafeArrays as JSON arrays |

The rate snapping, timecode rewrite, sensor-rate fallback and audio
reading are copied from PAD Forge's `agent-ffmpeg/brawdump/main.cpp`
(`snapRate`, `normaliseTimecode`, `readSensorRate`, `readAudioInfo`) so
both tools agree. The one family left out of `metadata` is
`post_3dlut_*_data` — the embedded 3D LUT itself (~430 000 bytes); its
name, title, size and mode are kept.

stderr carries diagnostics, `brawprobe: key=value ...`.

## Exit codes

brawdump's:

| code | meaning |
|---|---|
| 0 | ok |
| 2 | usage |
| 3 | SDK init: no library could be loaded, or no codec created |
| 4 | the SDK refused the clip: open, geometry, a frame rate that cannot be snapped, no finite positive sensor rate on frames 0–7, or a frame read that did not complete within 60 s |
| 5 | the JSON could not be written |

## Building

Nothing of the SDK is committed: the headers and
`BlackmagicRawAPIDispatch.cpp` are compiled in place from `SDK_DIR`
(the platform folder holding `Include/` and `Libraries/`), and the
library is loaded at run time.

**Portal server (Linux, the Blackmagic RAW SDK RPM 6.0):**

```sh
make -C tools/brawprobe          # SDK_DIR=/usr/lib64/blackmagic/BlackmagicRAWSDK/Linux
install -m 0755 tools/brawprobe/brawprobe /usr/local/bin/brawprobe
```

Linux links `-ldl -pthread`; the SDK's strings are `const char*` there.

**macOS (development, against NAS clips):**

```sh
make -C tools/brawprobe SDK_DIR="/Applications/Blackmagic RAW/Blackmagic RAW SDK/Mac"
tools/brawprobe/brawprobe "/Volumes/PAD_Storage/…/A001_C001.braw" | python3 -m json.tool
```

macOS links CoreFoundation; the SDK's strings are `CFStringRef`. The one
source builds on both through a small string shim at the top of
`brawprobe.cpp`.

`make probe CLIP=…` builds and runs on one clip; `make clean` removes
the binary and `build/`.

## Measured

On the Mac (Blackmagic RAW SDK 6.0), 2026-10-06, all exit 0:

| body | clip | geometry | fps / sensor | audio |
|---|---|---|---|---|
| Cinema Camera 6K | `0979-Gimbal-T1096_09091448_C004` | 6048×3200 | 25 / 48, off-speed | 2 ch, 48 kHz, 24-bit |
| URSA Mini Pro 12K | `0979-12K-T1273_09100925_C001` | 7680×4320 | 25 / 50, off-speed | 2 ch |
| PYXIS 12K | `1542-PyxisT2055_02210929_C001` | 8192×5360 | 25 / 72, off-speed | none |
| Pocket Cinema Camera 6K Pro | `1543-6k-600050_01190744_C001` | 6144×3456 | 25 / 50, off-speed | 2 ch |
| URSA Broadcast G2 | `A009_09062022_C001` | 3840×2160 | 25 / 25 | 2 ch |

`camera_id` is identical across `C004`, `C005` and `C006` of the 6K
while `clip_number` differs: it identifies the camera, not the clip.
These outputs are the provider's test fixtures (`tests/fixtures/braw/`).
