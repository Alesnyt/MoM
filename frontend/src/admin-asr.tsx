import { useEffect, useState } from "react";
import { listAsrModels, saveAsr, testAsr } from "./api";
import { encodeAsrModel, formatAsrLabel, parseAsrModel, WHISPER_SIZES, type AsrEngine } from "./asr";
import type { Health } from "./types";

export function AsrSettings({ health, onChange }: { health: Health | null; onChange: (health: Health) => void }) {
  const saved = health?.asr;
  const active = saved?.engine || health?.openai.asr_model || "";
  const [asrModel, setAsrModel] = useState(active || "local-whisper");
  const [url, setUrl] = useState(saved?.base_url || "");
  const [model, setModel] = useState(saved?.model || "");
  const [key, setKey] = useState("");
  const [catalog, setCatalog] = useState<string[] | null>(null);
  const [busy, setBusy] = useState<"list" | "test" | "save" | null>(null);
  const [error, setError] = useState("");
  const [note, setNote] = useState("");
  const asr = parseAsrModel(asrModel);
  const remote = asr.engine === "remote";
  const canConnect = Boolean(url.trim() && (key.trim() || saved?.key_set));

  useEffect(() => {
    setAsrModel(active || "local-whisper");
    setUrl(saved?.base_url || "");
    setModel(saved?.model || "");
    setCatalog(null);
  }, [active, saved?.base_url, saved?.model]);

  function invalidateCatalog() {
    setCatalog(null);
    setNote("");
    setError("");
  }

  function setEngine(engine: AsrEngine) {
    invalidateCatalog();
    setAsrModel(encodeAsrModel({ ...asr, engine, cloudModel: asr.cloudModel || "whisper-1" }));
  }

  async function perform(action: "list" | "test" | "save") {
    if (busy) return;
    setBusy(action);
    setError("");
    setNote("");
    const draft = { base_url: url.trim(), api_key: key.trim(), model: model.trim() };
    try {
      if (action === "list") {
        const models = await listAsrModels(draft);
        setCatalog(models);
        if (models.length && !models.includes(model)) setModel(models.length === 1 ? models[0] : "");
        if (!models.length) setNote("Сервер не перечислил модели расшифровки. Если имя модели известно, введите его вручную.");
      } else if (action === "test") {
        await testAsr(draft);
        setNote("Модель приняла тестовый аудиофайл.");
      } else {
        onChange(await saveAsr(asrModel.trim(), remote ? draft : undefined));
        setKey("");
        setNote("Настройки расшифровки сохранены.");
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Не удалось выполнить запрос.");
    } finally {
      setBusy(null);
    }
  }

  return (
    <form className="key-box" onSubmit={e => { e.preventDefault(); void perform("save"); }}>
      <p>Текущий движок — {formatAsrLabel(active) || "не задан"}{active === "remote-api" && saved?.model ? ` · ${saved.model}` : ""}</p>
      <fieldset disabled={busy !== null} className="asr-fields">
        <div className="choice-grid">
          <button
            type="button"
            className={`choice-card ${asr.engine === "whisper" ? "active" : ""}`}
            onClick={() => setEngine("whisper")}
          >
            <strong>Whisper</strong>
            <small>Локально, CPU, меньше RAM</small>
          </button>
          <button
            type="button"
            className={`choice-card ${asr.engine === "gigaam" ? "active" : ""}`}
            onClick={() => setEngine("gigaam")}
          >
            <strong>GigaAM</strong>
            <small>Сбер, локально, 2026</small>
          </button>
          <button
            type="button"
            className={`choice-card ${asr.engine === "cloud" ? "active" : ""}`}
            onClick={() => setEngine("cloud")}
          >
            <strong>Облако</strong>
            <small>Qwen ASR или whisper-1</small>
          </button>
          <button
            type="button"
            className={`choice-card ${asr.engine === "remote" ? "active" : ""}`}
            onClick={() => setEngine("remote")}
          >
            <strong>Свой API</strong>
            <small>GigaAM на GPU или другая совместимая модель</small>
          </button>
        </div>
        <div className="field-stack" style={{ marginTop: 14 }}>
          {remote && (
            <>
              <label className="field-label">
                Адрес API расшифровки
                <input className="title-input" type="url" required placeholder="https://asr.company.ru/v1"
                  value={url} onChange={e => { setUrl(e.target.value); invalidateCatalog(); }} />
              </label>
              <label className="field-label">
                Bearer-ключ
                <input className="title-input" type="password" autoComplete="new-password"
                  placeholder={health?.asr?.key_set ? "Ключ сохранён" : "Введите ключ без префикса Bearer"}
                  value={key} onChange={e => { setKey(e.target.value); invalidateCatalog(); }} />
              </label>
              <div><button type="button" className="ghost" disabled={!canConnect}
                onClick={() => void perform("list")}>{busy === "list" ? "Загружаю…" : "Загрузить модели"}</button></div>
              <label className="field-label">
                Модель расшифровки
                {catalog?.length ? (
                  <select className="title-input" required value={model} onChange={e => { setModel(e.target.value); setNote(""); }}>
                    <option value="" disabled>Выберите модель</option>
                    {catalog.map(id => <option key={id} value={id}>{id}</option>)}
                  </select>
                ) : (
                  <input className="title-input" required value={model} placeholder="Имя модели на сервере"
                    onChange={e => { setModel(e.target.value); setNote(""); }} />
                )}
              </label>
              <small>Для автоматической разметки спикеров модель должна возвращать сегменты с таймкодами.</small>
            </>
          )}
          {asr.engine === "whisper" && (
            <label className="field-label">
              Модель Whisper
              <select
                className="title-input"
                value={asr.whisperSize}
                onChange={(event) => {
                  setAsrModel(encodeAsrModel({ ...asr, whisperSize: event.target.value }));
                }}
              >
                {WHISPER_SIZES.map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.label}
                  </option>
                ))}
              </select>
            </label>
          )}
          {asr.engine === "gigaam" && (
            <>
              <label className="field-label">
                Модель GigaAM
                <select
                  className="title-input"
                  value={asr.gigaamVariant}
                  onChange={(event) => {
                    setAsrModel(
                      encodeAsrModel({
                        ...asr,
                        gigaamVariant: event.target.value === "large" ? "large" : "ctc",
                      }),
                    );
                  }}
                >
                  <option value="ctc">220M CTC — быстрее, меньше RAM</option>
                  <option value="large">600M CTC — точнее, тяжелее</option>
                </select>
              </label>
              <p>
                Пакеты и код модели уже в приложении. При первой расшифровке с Hugging Face скачиваются только веса.
              </p>
            </>
          )}
          {asr.engine === "cloud" && (
            <label className="field-label">
              Облачная модель
              <input
                className="title-input"
                placeholder="qwen3-asr-flash или whisper-1"
                value={asr.cloudModel}
                onChange={(event) => {
                  setAsrModel(encodeAsrModel({ ...asr, cloudModel: event.target.value }));
                }}
              />
              <small>Qwen ASR в текущем подключении возвращает текст без таймкодов. Для автоматической разметки спикеров выберите Whisper или GigaAM.</small>
            </label>
          )}
        </div>
        <p className="progress-hint">Для автоматического разделения голосов в MoM нужны HF_TOKEN и зависимости из requirements-diarize.txt. Имена спикеров можно уточнить во вкладке «Транскрипт».</p>
        <div className="composer-row">
          {remote && <button type="button" className="ghost" disabled={!canConnect || !model.trim()}
            onClick={() => void perform("test")}>{busy === "test" ? "Проверяю…" : "Проверить модель"}</button>}
          <button className="primary" type="submit" disabled={!asrModel.trim() || (remote && (!canConnect || !model.trim()))}>
            {busy === "save" ? "Сохраняю…" : "Сохранить расшифровку"}
          </button>
        </div>
      </fieldset>
      {note && <p role="status" className="progress-hint">{note}</p>}
      {error && <div role="alert" className="banner">{error}</div>}
    </form>
  );
}
