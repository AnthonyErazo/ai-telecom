import { GoogleGenAI, Modality, type LiveServerMessage, type Session } from "@google/genai";
import { api } from "../api/client";
import type { Explanation, LiveToken } from "../api/types";
import { MicrophoneStream, PcmAudioPlayer } from "./audio";

export type LiveStatus = "idle" | "connecting" | "listening" | "consulting" | "speaking" | "error";

export interface LiveEvents {
  onStatus: (status: LiveStatus) => void;
  onInputTranscript: (text: string) => void;
  onOutputTranscript: (text: string) => void;
  onExplanation: (explanation: Explanation) => void;
  onError: (message: string) => void;
}

export interface LiveContext {
  authToken: string;
  accountId: string;
  conversationId?: string;
  detail: string;
}

function blocksToText(explanation: Explanation): string {
  return explanation.bloques.flatMap((block) => {
    const title = block.titulo ? [block.titulo] : [];
    if (block.tipo === "texto" || block.tipo === "aviso") return [...title, block.texto];
    if (block.tipo === "kv") return [...title, ...block.items.map((item) => `${item.clave}: ${item.valor}`)];
    if (block.tipo === "tabla") return [...title, ...block.filas.map((row) => row.join(" · "))];
    return title;
  }).filter(Boolean).join("\n");
}

export class GeminiLiveClient {
  private session?: Session;
  private readonly microphone = new MicrophoneStream();
  private readonly player = new PcmAudioPlayer();
  private closed = false;

  constructor(private readonly context: LiveContext, private readonly events: LiveEvents) {}

  async connect(): Promise<void> {
    this.events.onStatus("connecting");
    const ephemeral: LiveToken = await api.liveToken(this.context.authToken);
    const ai = new GoogleGenAI({ apiKey: ephemeral.token, httpOptions: { apiVersion: "v1beta" } });
    this.session = await ai.live.connect({
      model: ephemeral.model,
      config: {
        responseModalities: [Modality.AUDIO],
        sessionResumption: {},
        inputAudioTranscription: {},
        outputAudioTranscription: {},
      },
      callbacks: {
        onopen: () => this.events.onStatus("listening"),
        onmessage: (message) => { void this.handleMessage(message); },
        onerror: (event) => {
          this.events.onStatus("error");
          this.events.onError(event.message || "Falló la conexión con Gemini Live");
        },
        onclose: () => { if (!this.closed) this.events.onStatus("idle"); },
      },
    });
    await this.microphone.start((data) => {
      this.session?.sendRealtimeInput({ audio: { data, mimeType: "audio/pcm;rate=16000" } });
    });
    this.events.onStatus("listening");
  }

  private async handleMessage(message: LiveServerMessage): Promise<void> {
    const content = message.serverContent;
    if (content?.interrupted) this.player.interrupt();
    const input = content?.inputTranscription?.text;
    const output = content?.outputTranscription?.text;
    if (input) this.events.onInputTranscript(input);
    if (output) this.events.onOutputTranscript(output);

    for (const part of content?.modelTurn?.parts ?? []) {
      const audio = part.inlineData;
      if (audio?.data && audio.mimeType?.startsWith("audio/")) {
        this.events.onStatus("speaking");
        await this.player.play(audio.data);
      }
    }
    if (content?.turnComplete) this.events.onStatus("listening");

    for (const call of message.toolCall?.functionCalls ?? []) {
      await this.executeTool(call.id, call.name, call.args ?? {});
    }
  }

  private async executeTool(id: string | undefined, name: string | undefined, args: Record<string, unknown>): Promise<void> {
    if (!this.session || name !== "explicar_recibo") return;
    this.events.onStatus("consulting");
    try {
      const explanation = await api.explain(this.context.authToken, {
        conversation_id: this.context.conversationId,
        cuenta_id: this.context.accountId,
        periodo: typeof args.periodo === "string" ? args.periodo : undefined,
        verbosidad: args.verbosidad === "DETALLE" ? "DETALLE" : this.context.detail,
        utterance: typeof args.pregunta === "string" ? args.pregunta : "Explique mi recibo",
      });
      this.context.conversationId = explanation.conversation_id;
      this.events.onExplanation(explanation);
      this.session.sendToolResponse({
        functionResponses: {
          id,
          name,
          response: {
            output: {
              respuesta_verificada: blocksToText(explanation),
              verificacion_numerica: explanation.gobernanza.verificacion_numerica,
              aserciones_no_ancladas: explanation.gobernanza.aserciones_no_ancladas,
              factset_sha256: explanation.gobernanza.factset_sha256,
              trace_id: explanation.trace_id,
            },
          },
        },
      });
    } catch (error) {
      this.session.sendToolResponse({
        functionResponses: { id, name, response: { error: error instanceof Error ? error.message : "Error del backend" } },
      });
      this.events.onError(error instanceof Error ? error.message : "No se pudo consultar el recibo");
    }
  }

  async close(): Promise<void> {
    this.closed = true;
    this.session?.sendRealtimeInput({ audioStreamEnd: true });
    await this.microphone.stop();
    await this.player.close();
    this.session?.close();
    this.session = undefined;
    this.events.onStatus("idle");
  }
}
