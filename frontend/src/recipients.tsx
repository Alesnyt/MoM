import { useState } from "react";
import type { Meeting, ProtocolRecipient, Speaker } from "./types";

type Choice = { identity: string; selected: boolean; email: string };
const identity = (speaker: Speaker) => JSON.stringify([speaker.name, speaker.email || ""]);

export function ProtocolRecipients({ meeting, mailEnabled, disabled, busy, onSubmit }: {
  meeting: Meeting;
  mailEnabled: boolean;
  disabled: boolean;
  busy: boolean;
  onSubmit: (recipients: ProtocolRecipient[]) => Promise<void>;
}) {
  const [choices, setChoices] = useState<Record<string, Choice>>({});
  const speakers = (meeting.speakers || []).filter((speaker) => speaker.id !== "unknown" && speaker.name !== "Неясно");
  const rows = speakers.map((speaker) => {
    const saved = choices[speaker.id];
    // A renamed/reset speaker must be selected again to avoid sending to a stale contact.
    const choice = saved?.identity === identity(speaker) ? saved : {
      identity: identity(speaker), selected: Boolean(speaker.send_protocol), email: speaker.email || "",
    };
    return { speaker, choice };
  });
  const selected = mailEnabled ? rows.filter(({ choice }) => choice.selected) : [];
  const invalidEmail = selected.some(({ choice }) => !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(choice.email.trim()));

  function change(speaker: Speaker, choice: Choice, fields: Partial<Choice>) {
    setChoices((current) => ({ ...current, [speaker.id]: { ...choice, ...fields } }));
  }

  return (
    <form className="protocol-delivery" onSubmit={(event) => {
      event.preventDefault();
      if (!disabled && !invalidEmail) void onSubmit(selected.map(({ speaker, choice }) => ({
        speaker_id: speaker.id, email: choice.email.trim(),
      })));
    }}>
      <fieldset className="protocol-recipients" disabled={disabled || !mailEnabled}>
        <legend>Кому ещё отправить протокол</legend>
        <p className="progress-hint">{mailEnabled
          ? "Вам письмо придёт автоматически. Отметьте дополнительных получателей: им протокол отправится после сборки. Выбор сохраняется при нажатии «Собрать протокол»."
          : "Отправка писем пока не настроена. Администратор может включить её в настройках SMTP. Протокол можно собрать без рассылки."}</p>
        <div className="recipient-list">
          {rows.map(({ speaker, choice }) => (
            <div className="recipient-row" key={speaker.id}>
              <label className="recipient-select">
                <input type="checkbox" checked={choice.selected && mailEnabled}
                  onChange={(event) => change(speaker, choice, { selected: event.target.checked })} />
                <span>{speaker.name}</span>
              </label>
              <input className="title-input" type="email" maxLength={254}
                aria-label={`Email для ${speaker.name}`} placeholder="Email получателя"
                value={choice.email} required={choice.selected && mailEnabled}
                disabled={disabled || !mailEnabled || !choice.selected}
                onChange={(event) => change(speaker, choice, { email: event.target.value })} />
            </div>
          ))}
        </div>
        {!speakers.length && <p className="progress-hint">Получателей можно выбрать после разметки спикеров.</p>}
      </fieldset>
      {invalidEmail && <p className="progress-hint" role="status">Укажите email каждого выбранного спикера.</p>}
      <button className="primary" type="submit" disabled={disabled || invalidEmail}>
        {busy ? "Ставлю в очередь…" : "Собрать протокол"}
      </button>
    </form>
  );
}
