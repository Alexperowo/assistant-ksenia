// Запись микрофона планшета: частота звуковой карты (обычно 48 кГц) -> 16 кГц моно, кадры по 20 мс (320 отсчётов).
// Усреднение по окну — простой фильтр от наложения частот; для распознавания речи этого достаточно.
class Recorder extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / 16000;
    this.pos = 0;
    this.acc = 0;
    this.cnt = 0;
    this.out = new Int16Array(320);
    this.n = 0;
  }

  process(inputs) {
    const ch = inputs[0] && inputs[0][0];
    if (!ch) return true;
    for (let i = 0; i < ch.length; i++) {
      this.acc += ch[i];
      this.cnt += 1;
      this.pos += 1;
      if (this.pos >= this.ratio) {
        this.pos -= this.ratio;
        const v = this.acc / this.cnt;
        this.acc = 0;
        this.cnt = 0;
        this.out[this.n++] = Math.max(-1, Math.min(1, v)) * 32767;
        if (this.n === this.out.length) {
          this.port.postMessage(this.out.slice(0));
          this.n = 0;
        }
      }
    }
    return true;
  }
}

registerProcessor('recorder', Recorder);
