export class PhoneAudio {
  constructor(onError){
    if(!window.AudioDecoder)throw Error('Браузер не поддерживает декодирование звука');
    this.ctx=new AudioContext({sampleRate:48000,latencyHint:'interactive'});
    this.ctx.resume();this.next=0;
    this.decoder=new AudioDecoder({output:frame=>{
      const buffer=this.ctx.createBuffer(frame.numberOfChannels,frame.numberOfFrames,frame.sampleRate);
      for(let c=0;c<frame.numberOfChannels;c++)frame.copyTo(buffer.getChannelData(c),{planeIndex:c,format:'f32-planar'});
      frame.close();
      if(this.next>this.ctx.currentTime+.3)this.next=this.ctx.currentTime;
      const source=this.ctx.createBufferSource();source.buffer=buffer;source.connect(this.ctx.destination);
      this.next=Math.max(this.ctx.currentTime+.015,this.next);source.start(this.next);this.next+=buffer.duration;
    },error:onError});
  }
  feed(flags,data){
    if(flags&(1n<<63n)){
      const frequencies=[96000,88200,64000,48000,44100,32000,24000,22050,16000,12000,11025,8000,7350];
      const rateIndex=((data[0]&7)<<1)|(data[1]>>7),channels=(data[1]>>3)&15;
      this.decoder.configure({codec:'mp4a.40.2',sampleRate:frequencies[rateIndex]||48000,numberOfChannels:channels||2,description:data});
    }else if(this.decoder.state==='configured'){
      this.decoder.decode(new EncodedAudioChunk({type:'key',timestamp:Number(flags&((1n<<62n)-1n)),data}));
    }
  }
  close(){try{this.decoder.close()}catch{}this.ctx.close()}
}
