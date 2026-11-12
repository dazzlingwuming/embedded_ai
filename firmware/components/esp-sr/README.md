# ESP-SR 2.6.2: P4 acoustic DSP subset

This component contains only the ESP32-P4 revision 3 AEC / WebRTC VAD API headers
and the official DSP archive needed by the speech frontend. It does not contain
or load wake-word, speech-recognition, or speech-synthesis language models.

Files are unmodified members of the official Espressif Component Registry
`esp-sr` 2.6.2 archive. `UPSTREAM.json` records each file's official SHA-256, size,
and immutable archive/checksum URLs. The upstream license is retained in LICENSE.

Reproduce downloads from the workspace root with the project Python:

```powershell
.\.tools\python\Scripts\python.exe scripts\prepare-speech-dsp.py
```

The downloader reads the registry ZIP directory and only selected members using
small HTTP ranges, validates ZIP CRC and registry SHA-256, and keeps all caches
under `.cache/speech-dsp/`. The local CMake wrapper links the acoustic DSP archive
and the pinned `dl_fft` support component; unreferenced archive members are not
linked into firmware.
