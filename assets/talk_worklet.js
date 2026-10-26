/*
 * AudioWorklet for the talking AI's ears: the microphone as 16 kHz mono 16-bit PCM, posted to
 * the page in 100 ms chunks (talk.js sends them to the hub's /api/talk/stt, where Whisper
 * listens). As in reflex_ddns_livekit_english_chat's subtitle_worklet.js.
 */
class TalkPcm extends AudioWorkletProcessor {
  constructor() {
    super();
    this.step = sampleRate / 16000; // input samples per output sample
    this.pos = 0;
    this.sum = 0;
    this.count = 0;
    this.chunk = new Int16Array(1600);
    this.length = 0;
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (!channel) return true;
    for (let i = 0; i < channel.length; i++) {
      this.sum += channel[i];
      this.count += 1;
      this.pos += 1;
      if (this.pos < this.step) continue;
      this.pos -= this.step;
      // The mean of the input samples it covers: a simple low-pass against aliasing.
      const v = Math.max(-1, Math.min(1, this.sum / this.count));
      this.sum = 0;
      this.count = 0;
      this.chunk[this.length++] = v < 0 ? v * 0x8000 : v * 0x7fff;
      if (this.length === this.chunk.length) {
        this.port.postMessage(this.chunk.buffer, [this.chunk.buffer]);
        this.chunk = new Int16Array(1600);
        this.length = 0;
      }
    }
    return true;
  }
}

registerProcessor('talk-pcm', TalkPcm);
