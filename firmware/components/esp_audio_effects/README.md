# esp_audio_effects 1.4.5: P4 rate conversion

The API headers and DSP archive are unmodified official registry 1.4.5 files,
verified against the SHA-256 values in `UPSTREAM.json`. The upstream Espressif
license is retained in LICENSE. This firmware references sample-rate conversion
only; unrelated effect objects in the archive are not linked into firmware.

The same project-local `scripts/prepare-speech-dsp.py` reproduces these files
without downloading other chip targets. This wrapper intentionally omits the
full component's FFT dependency, which is unnecessary for the referenced FIR
sample-rate conversion symbols; the final firmware link verifies this boundary.
