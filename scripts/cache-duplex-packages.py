"""Prefetch public Espressif archives using bounded range requests.

The component manager subsequently verifies unpacked content against the lock
file. Requests connect directly and ignore environment proxy settings. No
credentials, TLS bypass, global cache, or SDK source patch is used.
"""
import asyncio
from pathlib import Path
import re

import aiohttp

ROOT = Path(__file__).resolve().parent.parent
DESTINATION = ROOT/'.cache'/'duplex-reference'
PACKAGES = [('esp-sr','2.6.2'),('esp_audio_effects','1.4.5'),('gmf_fft','1.1.0')]


async def main():
    DESTINATION.mkdir(parents=True,exist_ok=True)
    semaphore=asyncio.Semaphore(4)
    timeout=aiohttp.ClientTimeout(total=50,connect=10,sock_read=20)
    async with aiohttp.ClientSession(trust_env=False,timeout=timeout) as client:
        for name,version in PACKAGES:
            url=f'https://components-file.espressif.com/components/espressif/{name}/{version}/espressif__{name}-v{version}.zip'
            async with client.get(url,proxy=None,headers={'Range':'bytes=0-0'}) as response:
                match=re.fullmatch(r'bytes 0-0/(\d+)',response.headers.get('Content-Range',''))
                if response.status!=206 or not match:
                    raise RuntimeError('Official archive did not confirm range support')
                total=int(match.group(1));await response.read()
            target=DESTINATION/f'{name}-{version}.zip'
            complete=target.with_suffix('.complete')
            if complete.exists() and target.stat().st_size==total:
                print(f'{name}: archive already downloaded',flush=True);continue
            with target.open('wb') as output: output.truncate(total)
            downloaded=0
            async def download(offset):
                nonlocal downloaded
                end=min(total-1,offset+4*1024*1024-1)
                async with semaphore:
                    for attempt in range(4):
                        try:
                            async with client.get(url,proxy=None,
                                    headers={'Range':f'bytes={offset}-{end}'}) as response:
                                if response.status!=206 or response.headers.get('Content-Range')!=f'bytes {offset}-{end}/{total}':
                                    raise RuntimeError('Archive range mismatch')
                                data=await response.read()
                                if len(data)!=end-offset+1:raise RuntimeError('Incomplete archive range')
                            with target.open('r+b') as output:output.seek(offset);output.write(data)
                            downloaded+=len(data)
                            print(f'{name}: {downloaded/1048576:.1f}/{total/1048576:.1f} MiB',flush=True)
                            return
                        except (aiohttp.ClientError,TimeoutError,RuntimeError):
                            if attempt==3:raise
                            await asyncio.sleep(1+attempt)
            await asyncio.gather(*(download(offset) for offset in range(0,total,4*1024*1024)))
            complete.write_text('range download complete; component hash verification follows\n',encoding='utf-8')


if __name__=='__main__':asyncio.run(main())
