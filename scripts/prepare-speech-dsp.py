"""Fetch a verified P4 DSP-only subset from immutable official registry archives.

Uses ZIP range reads so unrelated targets and offline language models are not
downloaded. Every selected file is checked against registry SHA-256 metadata.
"""
import asyncio
import hashlib
import json
from pathlib import Path
import re
import struct
import zlib

import aiohttp

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT/'.cache'/'speech-dsp'
COMPONENTS = ROOT/'firmware'/'components'
PROXY = None  # User-selected Direct networking; never change system proxy/Wi-Fi.
CHUNK = 65536


class RegistryZip:
    def __init__(self, session, name, version, concurrency):
        self.session, self.name, self.version = session, name, version
        self.semaphore = concurrency
        self.base = f'https://components-file.espressif.com/components/espressif/{name}/{version}/'
        self.url = self.base+f'espressif__{name}-v{version}.zip'
        self.cache = CACHE/f'{name}-{version}'
        self.cache.mkdir(parents=True, exist_ok=True)

    async def fetch(self, start, end):
        target = self.cache/f'range-{start}-{end}.bin'
        if target.is_file() and target.stat().st_size == end-start+1:
            return target.read_bytes()
        async with self.semaphore:
            for attempt in range(5):
                try:
                    async with self.session.get(self.url, proxy=PROXY,
                            headers={'Range':f'bytes={start}-{end}'}) as response:
                        if response.status != 206:
                            raise RuntimeError(f'Range HTTP {response.status}')
                        actual = response.headers.get('Content-Range', '')
                        if not re.fullmatch(fr'bytes {start}-{end}/\d+', actual):
                            raise RuntimeError(f'Unexpected range {actual}')
                        data = await response.read()
                        if len(data) != end-start+1:
                            raise RuntimeError('Incomplete archive range')
                    target.write_bytes(data)
                    return data
                except (aiohttp.ClientError, TimeoutError, RuntimeError):
                    if attempt == 4:
                        raise
                    await asyncio.sleep(1+attempt)

    async def region(self, start, length):
        results = await asyncio.gather(*(self.fetch(offset, min(start+length-1,offset+CHUNK-1))
            for offset in range(start,start+length,CHUNK)))
        return b''.join(results)

    async def initialize(self):
        checksums = self.cache/'CHECKSUMS.json'
        if not checksums.is_file():
            async with self.session.get(self.base+'CHECKSUMS.json', proxy=PROXY) as response:
                response.raise_for_status(); checksums.write_bytes(await response.read())
        self.checksums = {item['path']:item for item in json.loads(checksums.read_text())['files']}
        index = self.cache/'zip-index.json'
        if index.is_file():
            self.index = json.loads(index.read_text()); return
        async with self.session.get(self.url,proxy=PROXY,headers={'Range':'bytes=0-0'}) as response:
            total = int(response.headers['Content-Range'].rsplit('/',1)[1]); await response.read()
        tail = await self.fetch(max(0,total-65557), total-1)
        position=tail.rfind(b'PK\x05\x06')
        if position < 0:raise RuntimeError('Missing ZIP directory')
        _,disk,dir_disk,entries_disk,entries,size,offset,_ = struct.unpack_from('<4s4H2IH',tail,position)
        if disk or dir_disk or entries!=entries_disk:raise RuntimeError('Unsupported multipart archive')
        directory=await self.region(offset,size)
        self.index={};cursor=0
        for _ in range(entries):
            fields=struct.unpack_from('<4s6H3I5H2I',directory,cursor)
            if fields[0]!=b'PK\x01\x02':raise RuntimeError('Invalid ZIP directory record')
            name_length,extra_length,comment_length=fields[10:13]
            name=directory[cursor+46:cursor+46+name_length].decode('utf-8')
            self.index[name]={'offset':fields[16],'method':fields[4], 'crc':fields[7],
                              'compressed':fields[8],'size':fields[9]}
            cursor+=46+name_length+extra_length+comment_length
        index.write_text(json.dumps(self.index),encoding='utf-8')

    async def extract(self, relative, component):
        target=component/relative
        expected=self.checksums[relative]
        if target.is_file() and hashlib.sha256(target.read_bytes()).hexdigest()==expected['hash']:
            return
        entry=self.index[relative]
        header=await self.fetch(entry['offset'],entry['offset']+29)
        fields=struct.unpack('<4s5H3I2H',header)
        if fields[0]!=b'PK\x03\x04' or fields[2]&1:raise RuntimeError('Invalid encrypted ZIP member')
        begin=entry['offset']+30+fields[-2]+fields[-1]
        data=await self.region(begin,entry['compressed'])
        if entry['method']==8:data=zlib.decompress(data,-15)
        elif entry['method']!=0:raise RuntimeError('Unsupported ZIP method')
        if len(data)!=entry['size'] or zlib.crc32(data)!=entry['crc']:
            raise RuntimeError('ZIP member CRC/size mismatch')
        if hashlib.sha256(data).hexdigest()!=expected['hash']:
            raise RuntimeError('Registry SHA-256 mismatch: '+relative)
        target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data)
        print(f'{self.name}: {relative} ({len(data)} bytes) verified',flush=True)


async def main():
    semaphore=asyncio.Semaphore(16)
    timeout=aiohttp.ClientTimeout(total=35,connect=10,sock_read=15)
    async with aiohttp.ClientSession(trust_env=False,timeout=timeout) as session:
        sr=RegistryZip(session,'esp-sr','2.6.2',semaphore)
        ae=RegistryZip(session,'esp_audio_effects','1.4.5',semaphore)
        await asyncio.gather(sr.initialize(),ae.initialize())
        sr_files=['LICENSE','include/esp32p4/esp_aec.h','include/esp32p4/esp_aec_nlp.h',
                  'include/esp32p4/esp_vad.h','lib/esp32p4/libesp_audio_processor.a',
                  'lib/esp32p4/libdl_lib.a']
        ae_files=['LICENSE','include/esp_ae_rate_cvt.h','include/esp_ae_types.h',
                  'lib/esp32p4/libesp_audio_effects.a']
        await asyncio.gather(*(sr.extract(name,COMPONENTS/'esp-sr') for name in sr_files),
                            *(ae.extract(name,COMPONENTS/'esp_audio_effects') for name in ae_files))
        for archive,files in [(sr,sr_files),(ae,ae_files)]:
            evidence={'component':archive.name,'upstream_version':archive.version,
                'registry_archive':archive.url,'checksums_url':archive.base+'CHECKSUMS.json',
                'selection':'DSP only: no language/recognition/synthesis models',
                'files':{path:archive.checksums[path] for path in files}}
            target=COMPONENTS/archive.name/'UPSTREAM.json'
            target.write_text(json.dumps(evidence,indent=2),encoding='utf-8')


if __name__=='__main__':asyncio.run(main())
