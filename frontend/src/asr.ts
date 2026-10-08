export const WHISPER_SIZES = [
  { id: "tiny", label: "tiny — быстрее, хуже качество" },
  { id: "base", label: "base" },
  { id: "small", label: "small — по умолчанию" },
  { id: "medium", label: "medium — точнее, больше RAM" },
  { id: "large-v2", label: "large-v2" },
  { id: "large-v3", label: "large-v3 — максимум качества" },
] as const;

export type AsrEngine = "whisper" | "gigaam" | "cloud";

export type AsrChoice = {
  engine: AsrEngine;
  whisperSize: string;
  gigaamVariant: "ctc" | "large";
  cloudModel: string;
};

export function parseAsrModel(model: string): AsrChoice {
  const name = (model || "").trim().toLowerCase();
  if (name.startsWith("gigaam") || name === "sber" || name.startsWith("sber-")) {
    return {
      engine: "gigaam",
      whisperSize: "small",
      gigaamVariant: name.includes("large") ? "large" : "ctc",
      cloudModel: "",
    };
  }
  if (!name || name.startsWith("local") || name === "faster-whisper") {
    let size = "small";
    const tagged = name.match(/local-whisper-(.+)$/);
    const short = name.match(/^local-(tiny|base|small|medium|large-v2|large-v3)$/);
    if (tagged) size = tagged[1];
    else if (short) size = short[1];
    return { engine: "whisper", whisperSize: size, gigaamVariant: "ctc", cloudModel: "" };
  }
  return { engine: "cloud", whisperSize: "small", gigaamVariant: "ctc", cloudModel: model };
}

export function encodeAsrModel(choice: AsrChoice): string {
  if (choice.engine === "gigaam") {
    return choice.gigaamVariant === "large" ? "gigaam-multilingual-large" : "gigaam-multilingual";
  }
  if (choice.engine === "whisper") {
    return choice.whisperSize === "small" ? "local-whisper" : `local-whisper-${choice.whisperSize}`;
  }
  return choice.cloudModel.trim();
}

export function formatAsrLabel(model: string | null | undefined): string {
  if (!model) return "";
  const asr = parseAsrModel(model);
  if (asr.engine === "gigaam") {
    return asr.gigaamVariant === "large"
      ? "Сбер GigaAM Multilingual 600M"
      : "Сбер GigaAM Multilingual 220M";
  }
  if (asr.engine === "whisper") return `Whisper ${asr.whisperSize}`;
  return model;
}
