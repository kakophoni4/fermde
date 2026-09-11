"""scrcpy 3.3.4 framed H.264 -> WebCodecs, validated input -> control socket.

Closing a viewer only stops its scrcpy process, never the Android device.
"""
import asyncio
import contextlib
import json
import secrets
import struct
from fastapi import WebSocketDisconnect

viewers=set()

def control_packet(message,width,height):
    kind=message.get('type')
    if kind=='key':
        key=int(message['key'])
        if key not in (3,4,19,20,21,22,24,25,26,61,66,67,82,92,93,111,112,122,123,187):
            raise ValueError('Unsupported key')
        return b''.join(struct.pack('>BBIII',0,a,key,0,0) for a in (0,1))
    if kind=='text':
        text=str(message.get('text','')).encode('utf-8')
        if len(text)>16000: raise ValueError('Text too long')
        # Clipboard + paste supports Unicode, unlike Android input text.
        return struct.pack('>BQBI',9,0,1,len(text))+text
    if kind=='touch':
        action=int(message['action'])
        if action not in (0,1,2,3): raise ValueError('Unsupported touch action')
        x=max(0,min(width-1,round(float(message['x'])*width)))
        y=max(0,min(height-1,round(float(message['y'])*height)))
        return struct.pack('>BBQiiHHHII',2,action,0xffffffffffffffff,x,y,width,height,
                           0 if action in (1,3) else 65535,0,0)
    if kind=='scroll':
        x=max(0,min(width-1,round(float(message['x'])*width)))
        y=max(0,min(height-1,round(float(message['y'])*height)))
        v=max(-32767,min(32767,round(float(message['dy'])*2048)))
        return struct.pack('>BiiHHhhI',3,x,y,width,height,0,v,0)
    raise ValueError('Unknown input')

async def serve(ws,i,u):
    from fermde.app import adb, ADB, current, device_for
    if i in viewers:
        await ws.accept()
        await ws.close(code=4429,reason='Устройство уже открыто в другой вкладке'); return
    viewers.add(i)
    await ws.accept()
    serial=f'10.231.{i}.2:15555'
    scid=secrets.randbelow(0x7fffffff)
    socket_name=f'scrcpy_{scid:08x}'
    port=None; process=None; writers=[]; tasks=[]
    audio=ws.query_params.get('audio')=='1'
    quality=ws.query_params.get('quality','balanced')
    size,fps,bitrate={'fast':(1024,30,2000000),'balanced':(1600,60,8000000),'sharp':(2400,60,12000000)}.get(quality,(1600,60,8000000))
    try:
        await adb(serial,'push','/opt/fermde/vendor/scrcpy-server.jar','/data/local/tmp/fermde-scrcpy.jar')
        port=int(await adb(serial,'forward','tcp:0','localabstract:'+socket_name))
        process=await asyncio.create_subprocess_exec(ADB,'-s',serial,'shell',
            'CLASSPATH=/data/local/tmp/fermde-scrcpy.jar','app_process','/',
            'com.genymobile.scrcpy.Server','3.3.4',f'scid={scid:08x}',
            'tunnel_forward=true',f'audio={str(audio).lower()}','audio_codec=aac',
            'control=true','video_codec=h264',
            f'max_size={size}',f'max_fps={fps}',f'video_bit_rate={bitrate}',
            'send_device_meta=false','send_dummy_byte=false','send_codec_meta=true',
            'send_frame_meta=true','clipboard_autosync=false','stay_awake=true',
            stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
        async def connection():
            # ADB forwards may accept TCP before the abstract socket exists; avoid
            # connecting repeatedly and stealing the server's second/control socket.
            for _ in range(40):
                sockets=await adb(serial,'shell','cat','/proc/net/unix')
                if socket_name in sockets: break
                if process.returncode is not None: raise RuntimeError('scrcpy server stopped')
                await asyncio.sleep(.25)
            return await asyncio.open_connection('127.0.0.1',port)
        vr,vw=await connection(); writers.append(vw)
        ar=None
        if audio:
            ar,aw=await asyncio.open_connection('127.0.0.1',port);writers.append(aw)
        cr,cw=await asyncio.open_connection('127.0.0.1',port); writers.append(cw)
        meta=await asyncio.wait_for(vr.readexactly(12),20)
        codec,width,height=struct.unpack('>III',meta)
        if codec!=0x68323634 or not 1<=width<=8192 or not 1<=height<=8192:
            raise RuntimeError('Unsupported video metadata')
        await ws.send_json({'type':'size','width':width,'height':height})
        async def video():
            while True:
                header=await vr.readexactly(12)
                length=struct.unpack('>I',header[8:])[0]
                if length>16*1024*1024: raise RuntimeError('Oversize video packet')
                payload=await vr.readexactly(length)
                await asyncio.wait_for(ws.send_bytes(b'\x00'+header+payload),5)
        async def audio_frames():
            codec=struct.unpack('>I',await ar.readexactly(4))[0]
            if codec in (0,1):
                await ws.send_json({'type':'audio-error','message':'Android не предоставил звук. Видео продолжает работать.'})
                await asyncio.Future()
            if codec!=0x00616163: raise RuntimeError('Unsupported audio codec')
            while True:
                header=await ar.readexactly(12)
                length=struct.unpack('>I',header[8:])[0]
                if length>1024*1024: raise RuntimeError('Oversize audio packet')
                payload=await ar.readexactly(length)
                await asyncio.wait_for(ws.send_bytes(b'\x01'+header+payload),5)
        async def inputs():
            while True:
                raw=await ws.receive_text()
                if len(raw)>20000: raise ValueError('Message too large')
                packet=control_packet(json.loads(raw),width,height)
                cw.write(packet); await cw.drain()
        async def drain_control():
            while await cr.read(4096): pass
        async def authorize():
            while True:
                await asyncio.sleep(3)
                who=current(ws.cookies.get('fermde_session'))
                if not who: raise RuntimeError('Сессия завершена')
                device_for(who,i)
        tasks=[asyncio.create_task(f()) for f in (video,inputs,drain_control,authorize)]
        if audio: tasks.append(asyncio.create_task(audio_frames()))
        done,_=await asyncio.wait(tasks,return_when=asyncio.FIRST_COMPLETED)
        for task in done: task.result()
    except (WebSocketDisconnect,asyncio.IncompleteReadError): pass
    except Exception:
        with contextlib.suppress(Exception):
            await ws.send_json({'type':'error','message':'Передача экрана прервана. Переподключитесь; телефон продолжает работать.'})
    finally:
        for task in tasks: task.cancel()
        if tasks: await asyncio.gather(*tasks,return_exceptions=True)
        for w in writers: w.close()
        if process and process.returncode is None:
            process.terminate()
            with contextlib.suppress(Exception): await asyncio.wait_for(process.wait(),5)
        if port:
            with contextlib.suppress(Exception): await adb(serial,'forward','--remove',f'tcp:{port}')
        viewers.discard(i)
        with contextlib.suppress(Exception): await ws.close()
