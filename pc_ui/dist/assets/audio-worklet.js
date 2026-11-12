class PCMMonitor extends AudioWorkletProcessor {
  constructor() {
    super(); this.ring = new Float32Array(48000); this.write = 0; this.read = 0; this.available = 0; this.position = 0; this.ready = false;
    this.port.onmessage = ({data}) => {
      for (let i=0;i<data.length;i++) {this.ring[this.write]=data[i];this.write=(this.write+1)%this.ring.length;this.available++;}
      if(this.available>12000){const drop=this.available-4800;this.read=(this.read+drop)%this.ring.length;this.available=4800;this.port.postMessage({dropped:drop});}
      if(this.available>1536)this.ready=true;
    };
  }
  process(inputs,outputs) {
    const out=outputs[0][0], ratio=48000/sampleRate;
    for(let i=0;i<out.length;i++){
      if(!this.ready||this.available<2){out[i]=0;this.ready=false;continue;}
      const next=(this.read+1)%this.ring.length;
      out[i]=this.ring[this.read]*(1-this.position)+this.ring[next]*this.position;
      this.position+=ratio;
      while(this.position>=1&&this.available){this.position-=1;this.read=(this.read+1)%this.ring.length;this.available--;}
    }
    return true;
  }
}
registerProcessor('pcm-monitor',PCMMonitor);
