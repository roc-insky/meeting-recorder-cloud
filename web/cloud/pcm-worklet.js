// AudioWorklet：把麦克风的 Float32 采样按块转交给主线程（用于实时转写，不参与录音/上传）。
class PcmTap extends AudioWorkletProcessor {
  constructor() {
    super();
    this.block = new Float32Array(2048);
    this.filled = 0;
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (!channel) return true;
    let offset = 0;
    while (offset < channel.length) {
      const room = this.block.length - this.filled;
      const take = Math.min(room, channel.length - offset);
      this.block.set(channel.subarray(offset, offset + take), this.filled);
      this.filled += take;
      offset += take;
      if (this.filled === this.block.length) {
        this.port.postMessage(this.block.slice(0));
        this.filled = 0;
      }
    }
    return true;
  }
}

registerProcessor("pcm-tap", PcmTap);
