const INPUT_RATE = 16_000;
const OUTPUT_RATE = 24_000;

export function microphoneSupportError(): string | null {
  if (typeof window === "undefined" || typeof navigator === "undefined") {
    return "El micrófono no está disponible en este entorno.";
  }
  if (!window.isSecureContext) {
    return "El micrófono requiere HTTPS o abrir la aplicación desde localhost.";
  }
  if (!navigator.mediaDevices?.getUserMedia) {
    return "Este navegador no permite acceder al micrófono en el contexto actual.";
  }
  return null;
}

function microphoneAccessError(error: unknown): Error {
  if (!(error instanceof DOMException)) {
    return error instanceof Error ? error : new Error("No se pudo iniciar el micrófono.");
  }
  const messages: Record<string, string> = {
    NotAllowedError: "Permiso de micrófono denegado. Habilítelo en la configuración del navegador.",
    NotFoundError: "No se encontró ningún micrófono conectado.",
    NotReadableError: "El micrófono está siendo usado por otra aplicación o no se puede leer.",
    SecurityError: "El navegador bloqueó el micrófono por seguridad. Use HTTPS o localhost.",
  };
  return new Error(messages[error.name] ?? `No se pudo iniciar el micrófono: ${error.message}`);
}

function bytesToBase64(bytes: Uint8Array): string {
  let binary = "";
  for (let index = 0; index < bytes.length; index += 1) binary += String.fromCharCode(bytes[index]);
  return btoa(binary);
}

function base64ToBytes(encoded: string): Uint8Array {
  const binary = atob(encoded);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) bytes[index] = binary.charCodeAt(index);
  return bytes;
}

function resample(input: Float32Array, sourceRate: number, targetRate: number): Float32Array {
  if (sourceRate === targetRate) return input;
  const ratio = sourceRate / targetRate;
  const output = new Float32Array(Math.max(1, Math.round(input.length / ratio)));
  for (let index = 0; index < output.length; index += 1) {
    const position = index * ratio;
    const left = Math.floor(position);
    const right = Math.min(left + 1, input.length - 1);
    const weight = position - left;
    output[index] = input[left] * (1 - weight) + input[right] * weight;
  }
  return output;
}

function pcm16Base64(samples: Float32Array): string {
  const buffer = new ArrayBuffer(samples.length * 2);
  const view = new DataView(buffer);
  samples.forEach((sample, index) => {
    const limited = Math.max(-1, Math.min(1, sample));
    view.setInt16(index * 2, limited < 0 ? limited * 0x8000 : limited * 0x7fff, true);
  });
  return bytesToBase64(new Uint8Array(buffer));
}

export class MicrophoneStream {
  private context?: AudioContext;
  private media?: MediaStream;
  private processor?: ScriptProcessorNode;
  private source?: MediaStreamAudioSourceNode;
  private sink?: GainNode;

  async start(onChunk: (base64Pcm: string) => void): Promise<void> {
    const unsupported = microphoneSupportError();
    if (unsupported) throw new Error(unsupported);
    try {
      this.media = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
    } catch (error) {
      throw microphoneAccessError(error);
    }
    this.context = new AudioContext();
    await this.context.resume();
    this.source = this.context.createMediaStreamSource(this.media);
    this.processor = this.context.createScriptProcessor(4096, 1, 1);
    this.sink = this.context.createGain();
    this.sink.gain.value = 0;
    this.processor.onaudioprocess = (event) => {
      const mono = event.inputBuffer.getChannelData(0);
      onChunk(pcm16Base64(resample(mono, this.context?.sampleRate ?? INPUT_RATE, INPUT_RATE)));
    };
    this.source.connect(this.processor);
    this.processor.connect(this.sink);
    this.sink.connect(this.context.destination);
  }

  async stop(): Promise<void> {
    this.processor?.disconnect();
    this.source?.disconnect();
    this.sink?.disconnect();
    this.media?.getTracks().forEach((track) => track.stop());
    if (this.context && this.context.state !== "closed") await this.context.close();
    this.context = undefined;
    this.media = undefined;
    this.processor = undefined;
    this.source = undefined;
    this.sink = undefined;
  }
}

export class PcmAudioPlayer {
  private context?: AudioContext;
  private nextStart = 0;
  private sources = new Set<AudioBufferSourceNode>();

  async play(encoded: string): Promise<void> {
    this.context ??= new AudioContext({ sampleRate: OUTPUT_RATE });
    await this.context.resume();
    const bytes = base64ToBytes(encoded);
    const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
    const samples = Math.floor(bytes.byteLength / 2);
    const buffer = this.context.createBuffer(1, samples, OUTPUT_RATE);
    const channel = buffer.getChannelData(0);
    for (let index = 0; index < samples; index += 1) channel[index] = view.getInt16(index * 2, true) / 0x8000;
    const source = this.context.createBufferSource();
    source.buffer = buffer;
    source.connect(this.context.destination);
    const start = Math.max(this.context.currentTime, this.nextStart);
    source.start(start);
    this.nextStart = start + buffer.duration;
    this.sources.add(source);
    source.onended = () => this.sources.delete(source);
  }

  interrupt(): void {
    this.sources.forEach((source) => {
      try { source.stop(); } catch { /* ya terminó */ }
    });
    this.sources.clear();
    this.nextStart = this.context?.currentTime ?? 0;
  }

  async close(): Promise<void> {
    this.interrupt();
    if (this.context && this.context.state !== "closed") await this.context.close();
    this.context = undefined;
  }
}
